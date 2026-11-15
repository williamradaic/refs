"""Bring PDFs that are already in Dropbox fully into the library.

"Fully" means: read and looked up (the pipeline), filed under a work, and,
inside the dump, renamed to the house convention. `refs add` and the folder
watcher both end here, so a paper saved from the browser, dropped on the web
page, or saved into papers-dump in Finder is treated the same way.

Only one ingest runs at a time on this Mac, across processes (the web server,
its watcher, and any `refs add` started by hand). Without that, a file saved
into the dump by `refs add` would also be picked up by the watcher a few
seconds later, and the model would be paid twice to read it.
"""

from __future__ import annotations

import fcntl
import os
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Callable, Iterator

from . import budget, llm, pipeline, records, rename, works
from . import scan as scanner
from .config import Config

# How much of the end of a file to search for the PDF end marker.
TAIL_BYTES = 4096


@dataclass
class Ingested:
    """What happened to one file."""

    rel: str                     # where it is now (after any rename)
    file_hash: str
    read: dict | None = None     # the read record; None if it was not processed
    work: dict | None = None
    renamed_from: str | None = None
    skipped: str | None = None   # why it was left for later, if it was
    processed_now: bool = False  # True if the pipeline ran (as opposed to a cached read)
    issues: list[str] = field(default_factory=list)


@contextmanager
def exclusive(cfg: Config) -> Iterator[None]:
    """Hold the Mac-wide ingest lock (waits until any other ingest finishes).

    The lock is an OS file lock on a file next to the index, so it is released
    automatically if a process dies while holding it.
    """
    lock_path = cfg.index_db.parent / "ingest.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def in_dump(cfg: Config, rel: str) -> bool:
    return rel.startswith(cfg.dump.rstrip("/") + "/")


def looks_complete(cfg: Config, rel: str) -> bool:
    """True if the PDF seems fully written: it ends with the "%%EOF" marker.

    A browser or Dropbox may still be writing a file when it first appears;
    reading it then would cache a reading of half a document. A file that is
    not complete yet is skipped, and its next change triggers another look.
    """
    try:
        with cfg.absolute(rel).open("rb") as handle:
            if handle.read(5) != b"%PDF-":
                return False
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - TAIL_BYTES))
            return b"%%EOF" in handle.read()
    except OSError:
        return False


def unread_files(cfg: Config, conn: sqlite3.Connection, *, dump_only: bool = False) -> list[tuple[str, str]]:
    """Indexed files the pipeline has never seen, as (path, hash) pairs.

    A file counts as seen if it, or the version it replaced (an annotated copy
    is a new hash), has a read record. Files whose reading failed are not
    retried here: `refs process` retries those on request, so a persistent
    failure is not paid for on every change.
    """
    rows = conn.execute(
        "SELECT p.path, p.hash, f.supersedes FROM paths p JOIN files f USING(hash) "
        "WHERE f.status IS NULL ORDER BY p.path").fetchall()
    chosen: dict[str, str] = {}             # hash -> the path to process it from
    for row in rows:
        file_hash, rel = row["hash"], row["path"]
        if records.load_read(cfg, file_hash) is not None:
            continue
        if row["supersedes"] and records.load_read(cfg, row["supersedes"]) is not None:
            continue
        # One file in several places is read once; a copy in the dump is
        # preferred, because only there does the app rename it.
        if file_hash not in chosen or (in_dump(cfg, rel) and not in_dump(cfg, chosen[file_hash])):
            chosen[file_hash] = rel
    pairs = [(rel, h) for h, rel in chosen.items()]
    if dump_only:
        pairs = [(rel, h) for rel, h in pairs if in_dump(cfg, rel)]
    return sorted(pairs)


def ingest(cfg: Config, conn: sqlite3.Connection, files: list[tuple[str, str]], *,
           source_url: str | None = None, check_complete: bool = True,
           progress: Callable[[int, int], None] | None = None,
           budget_left: float | None = None) -> list[Ingested]:
    """Process, file and (inside the dump) rename the given (path, hash) pairs.

    The caller holds `exclusive(cfg)` and has scanned, so every path is indexed.
    Files that already have a read record are not read again (no cost).
    `check_complete=False` skips the "still being written?" test, for a file
    that was handed over explicitly (`refs add file.pdf`). `budget_left`, in
    dollars, stops reading once this run has spent it (the monthly limit on
    automatic reading, budget.py); None means no limit.
    """
    results = [Ingested(rel=rel, file_hash=h) for rel, h in files]
    api, no_key = None, False
    spent = 0.0                                 # dollars this run, for budget_left
    topics = pipeline.known_topics(cfg)

    # 1. Read and look up each file that has no read record yet.
    for done, item in enumerate(results):
        if progress is not None:
            progress(done, len(results))           # `progress(done, total)`, for the page
        item.read = pipeline.load_read(cfg, item.file_hash)
        if item.read is not None:
            continue
        if check_complete and not looks_complete(cfg, item.rel):
            item.skipped = "the file looks incomplete (still downloading?)"
            continue
        confidential = pipeline._is_confidential(cfg, item.rel)
        if api is None and not confidential and not no_key:
            try:
                api = llm.client()
            except SystemExit:                  # no API key stored
                no_key = True
        if api is None and not confidential:
            item.skipped = "no Anthropic API key (run `refs setup-key`)"
            continue
        if budget_left is not None and not confidential and spent + budget.PER_PDF > budget_left:
            item.skipped = "this month's reading limit is reached (read_monthly_cap)"
            continue
        item.read = pipeline.process_file(cfg, api, item.file_hash, item.rel, topics)
        item.processed_now = True
        item.issues = list(item.read["issues"])
        if not item.read.get("reused_reading"):
            cost = llm.cost(item.read.get("usage") or {})
            spent += cost
            budget.record(conn, cost)

    # 2. File everything under its work (and attach PDFs that a "wanted" work awaited).
    file_to_work = works.sync(cfg)
    all_works = works.load_all(cfg)
    for item in results:
        if item.read is None:
            continue
        item.work = all_works.get(file_to_work.get(item.file_hash, ""))
        if item.work and source_url and not item.work["user"].get("source_url"):
            item.work["user"]["source_url"] = source_url
            records.write_json(works.works_dir(cfg) / f"{item.work['id']}.json", item.work)

    # 3. Inside the dump, a verified paper gets its proper name straight away.
    for item in results:
        if item.work is None or not in_dump(cfg, item.rel):
            continue
        current = PurePosixPath(item.rel).name
        downloaded = current.startswith("added-")      # the app's own temporary name
        new_name = rename.proposed_name(item.work, current, allow_unverified=downloaded)
        if not new_name:
            continue
        is_wp = bool((item.read.get("reading") or {}).get("is_working_paper"))
        new_name = rename.free_name(cfg, item.rel, new_name, is_wp)
        if new_name and (new_rel := rename.apply(cfg, item.rel, new_name, item.file_hash)):
            item.renamed_from, item.rel = item.rel, new_rel

    # 4. Bring the index up to date with the renames and the new records.
    scanner.scan(cfg, conn)
    return results


def headline(item: Ingested) -> str:
    """One line describing a file's outcome, for logs and notifications."""
    name = PurePosixPath(item.rel).name
    if item.skipped:
        return f"{name}: skipped, {item.skipped}"
    if item.read is None:
        return f"{name}: not read"
    meta = pipeline.final_metadata(item.read)
    if not meta["title"]:
        kind = (item.read.get("reading") or {}).get("kind") or "unknown"
        return f"{name}: {item.read['status']} ({kind})"
    who = ", ".join(meta["surnames"][:3]) + (" et al." if len(meta["surnames"]) > 3 else "")
    return f"[{item.read['status']}] {who} ({meta['year']}) {meta['title']}"
