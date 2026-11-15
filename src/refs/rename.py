"""Renaming files to the house convention, with a journal so it can be undone.

Rules (from the plan):
  * only verified works are renamed: the name is built from registry metadata;
  * in place only: a rename never moves a file to another folder;
  * never inside folders other tools manage (canvas-sync, overleaf) or a Git repo;
  * every rename is appended to <state>/journal.jsonl and can be reversed.

`plan()` decides what a set of files should be called; the command line
(`refs rename`) and the web page's Review > renames both use it.
"""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from . import naming, records, works
from .config import Config


def journal_path(cfg: Config) -> Path:
    return cfg.absolute(cfg.state_dir) / "journal.jsonl"


def proposed_name(work: dict, current_name: str, *, allow_unverified: bool = False) -> str | None:
    """The canonical file name for a work, or None if it must not be renamed.

    `allow_unverified` is only for a file the app itself just downloaded under a
    temporary name: a name built from the model's reading beats "added-93bc.pdf".
    """
    meta = work.get("meta") or {}
    surnames = [a["family"] for a in meta.get("authors") or [] if a.get("family")]
    if not surnames or not meta.get("title"):
        return None
    if meta.get("status") not in {"verified", "confirmed"} and not allow_unverified:
        return None
    # Keep an existing "-ocr" marker: it says the file is a scan with added text.
    is_ocr = PurePosixPath(current_name).stem.lower().endswith("ocr")
    return naming.canonical_name(surnames, meta.get("year"), meta["title"], ocr=is_ocr)


def disambiguate(name: str, taken: set[str], is_working_paper: bool) -> str:
    """Pick a free name when two different PDFs of one paper sit in the same folder.

    The first file to claim a name keeps it (callers offer published versions
    first). A working-paper version then becomes "...-wp.pdf"; anything else,
    or a second working paper, gets "-2", "-3", ...
    """
    if name not in taken:
        return name
    stem = name.removesuffix(".pdf")
    if is_working_paper and f"{stem}-wp.pdf" not in taken:
        return f"{stem}-wp.pdf"
    number = 2
    while f"{stem}-{number}.pdf" in taken:
        number += 1
    return f"{stem}-{number}.pdf"


def free_name(cfg: Config, rel: str, new_name: str, is_working_paper: bool) -> str | None:
    """The name one file should get in its own folder, or None to leave it as is.

    Other PDFs already in the folder keep their names; the file's own current
    name does not count as taken, since renaming frees it. So a working paper
    already called "...-wp.pdf" next to the published "....pdf" stays put
    instead of becoming "...-2.pdf".
    """
    current = PurePosixPath(rel).name
    folder = cfg.absolute(rel).parent
    taken = {p.name for p in folder.glob("*.pdf")} - {current}
    final = disambiguate(new_name, taken, is_working_paper)
    return None if final == current else final


def blocked_reason(cfg: Config, rel: str) -> str | None:
    """Why this file must be left alone, or None if renaming is allowed."""
    parts = PurePosixPath(rel).parts[:-1]
    for folder in parts:
        if folder.lower() in {name.lower() for name in cfg.no_rename}:
            return f"inside a managed folder ({folder})"
    # Walk up from the file's folder: a .git anywhere above means a repository.
    folder = cfg.absolute(rel).parent
    while folder != cfg.dropbox_root and folder != folder.parent:
        if (folder / ".git").exists():
            return "inside a Git repository"
        folder = folder.parent
    return None


def apply(cfg: Config, rel: str, new_name: str, file_hash: str | None = None) -> str | None:
    """Rename one file in place. Returns the new relative path, or None if skipped."""
    old = cfg.absolute(rel)
    new = old.with_name(new_name)
    if old.name == new_name or blocked_reason(cfg, rel) or not old.exists():
        return None
    if new.exists():                      # never overwrite: another file has that name
        return None
    os.rename(old, new)
    new_rel = str(PurePosixPath(rel).with_name(new_name))
    journal(cfg, rel, new_rel, file_hash)
    return new_rel


def journal(cfg: Config, rel: str, new_rel: str, file_hash: str | None, action: str = "rename") -> None:
    """Record one rename, move or copy, so `undo` can reverse it."""
    journal_path(cfg).parent.mkdir(parents=True, exist_ok=True)
    with journal_path(cfg).open("a") as handle:
        handle.write(json.dumps({
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "from": rel, "to": new_rel, "hash": file_hash, "action": action,
        }, ensure_ascii=False) + "\n")


def to_trash(source: Path) -> Path:
    """Move a file to the macOS Trash under its own name. Returns where it went."""
    trash_dir = Path.home() / ".Trash"
    target = trash_dir / source.name
    number = 2
    while target.exists():                         # never overwrite something in the Trash
        target = trash_dir / f"{source.stem} {number}{source.suffix}"
        number += 1
    os.rename(source, target)                      # same disk: a move, not a copy
    return target


def undo(cfg: Config, count: int = 1) -> list[tuple[str, str, str]]:
    """Reverse the last `count` renames, moves or copies.

    Returns (original path, path it had, action). A rename or move is put back;
    a copy is sent to the Trash (not deleted, since you may have marked it up),
    and only while the original is still there, so undo never removes the
    last copy of a file.
    """
    if not journal_path(cfg).exists():
        return []
    lines = journal_path(cfg).read_text().splitlines()
    restored = []
    while lines and len(restored) < count:
        entry = json.loads(lines.pop())
        action = entry.get("action", "rename")
        current, original = cfg.absolute(entry["to"]), cfg.absolute(entry["from"])
        if action == "copy":
            if current.exists() and original.exists():
                to_trash(current)
                restored.append((entry["from"], entry["to"], action))
        elif current.exists() and not original.exists():
            os.rename(current, original)
            restored.append((entry["from"], entry["to"], action))
    journal_path(cfg).write_text("".join(line + "\n" for line in lines))
    return restored


# --- Planning renames for many files ------------------------------------------------

@dataclass
class Proposal:
    path: str           # Dropbox-relative path now
    new_name: str       # the name it would get (same folder)
    file_hash: str
    work_id: str


def plan(cfg: Config, conn: sqlite3.Connection, items: list[dict],
         all_works: dict[str, dict]) -> tuple[list[Proposal], list[str], list[tuple[str, str]]]:
    """What the files of these search results should be called.

    Returns (proposals, identical copies that would collide, [(path, why protected)]).
    Two files in one folder can want the same name: published versions choose
    first; an identical copy is reported, not renamed; a different version of
    the same paper gets a "-wp" or "-2" suffix.
    """
    candidates, protected = [], []
    for item in items:
        work = all_works.get(item["work_id"] or "")
        for loc in item["locations"]:
            name = PurePosixPath(loc["path"]).name
            new_name = work and proposed_name(work, name)
            if not new_name or new_name == name:
                continue
            if reason := blocked_reason(cfg, loc["path"]):
                protected.append((loc["path"], reason))
                continue
            file_hash = conn.execute("SELECT hash FROM paths WHERE path=?", (loc["path"],)).fetchone()[0]
            reading = (records.load_read(cfg, file_hash) or {}).get("reading") or {}
            candidates.append((loc["path"], new_name, file_hash, bool(reading.get("is_working_paper")),
                               work["id"]))

    proposals, copies = [], []
    taken: dict[str, dict[str, str]] = {}          # folder -> {name: hash that owns it}
    for rel, new_name, file_hash, is_wp, work_id in sorted(candidates, key=lambda c: (c[3], c[0])):
        folder = str(PurePosixPath(rel).parent)
        names = taken.setdefault(folder, {
            p.name: "" for p in cfg.absolute(folder).glob("*.pdf")})   # names already on disk
        if names.get(new_name) == file_hash:
            copies.append(rel)                     # same content as a file already planned
            continue
        # The file's own current name is not "taken": renaming frees it. (Counting
        # it turned "x-wp.pdf", next to the published "x.pdf", into "x-2.pdf".)
        current = PurePosixPath(rel).name
        others = {n for n, owner in names.items() if not (n == current and owner in ("", file_hash))}
        final = disambiguate(new_name, others, is_wp)
        if final == current:
            continue                               # already has the right name
        names[final] = file_hash
        proposals.append(Proposal(rel, final, file_hash, work_id))
    return proposals, copies, protected


def apply_many(cfg: Config, proposals: list[Proposal]) -> list[tuple[str, str]]:
    """Carry out proposals. Returns (old path, new path) for each one done.

    The old name is kept on the work ("former names"), so descriptive names
    you gave files stay searchable.
    """
    all_works = works.load_all(cfg)
    done = []
    for proposal in proposals:
        new_rel = apply(cfg, proposal.path, proposal.new_name, proposal.file_hash)
        if new_rel is None:
            continue
        done.append((proposal.path, new_rel))
        work = all_works.get(proposal.work_id)
        if work is not None:
            former = work["user"].setdefault("former_names", [])
            old_name = PurePosixPath(proposal.path).name
            if old_name not in former:
                former.append(old_name)
                records.write_json(works.works_dir(cfg) / f"{work['id']}.json", work)
    return done
