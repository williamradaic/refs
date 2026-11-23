"""The Inbox: papers outside the library folder that the library lacks.

Two kinds of folder hold them:

* **Inbox folders** (`inboxes` in config.toml): course folders and a course
  site's sync folder, which refs lists but does not index. A paper there is
  offered with its course, and its topic as a part, filled in from its folders
  (collection.suggested: "220a/demand-estimation-ii"). Taking it in moves it
  into the library, except out of a folder another tool manages (canvas-sync,
  Git, Overleaf), which would bring a moved file back: those are copied.
* **Shared collections' folders** (shared.py): a PDF a coauthor added. Taking
  it copies it into the library (the coauthors keep theirs), in that collection.

A PDF counts as in the library when the library folder holds the same file,
or a file of the same paper (a work refs knows from an earlier read). Files
the index does not know are judged on this Mac, for free (downloads.judge),
and only papers ("paper", "maybe") are listed.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import PurePosixPath

from . import collection, contexts, downloads, library, records, rename, shared, works
from .config import Config

PAPER_KINDS = records.LIBRARY_KINDS            # paper, book, chapter
LIMIT = 2000                                    # PDFs looked at per listing, at most


def _walk(cfg: Config, top: str):
    """Library-relative paths of the PDFs under one inbox folder."""
    skipped = {str(cfg.absolute(rel)) for rel in [cfg.state_dir, *cfg.exclude_paths]}
    start = cfg.absolute(top)
    if not start.is_dir():
        return
    for folder, subfolders, files in os.walk(start, followlinks=False):
        subfolders[:] = [n for n in subfolders if n.lower() not in cfg.ignore_dirs
                         and os.path.join(folder, n) not in skipped]
        for name in files:
            if name.lower().endswith(".pdf") and not name.startswith("._"):
                full = os.path.join(folder, name)
                if full not in skipped and os.path.isfile(full) and not os.path.islink(full):
                    yield os.path.relpath(full, cfg.dropbox_root).replace(os.sep, "/")


def _in_library(cfg: Config, conn: sqlite3.Connection, file_hash: str | None, work_id: str | None) -> bool:
    folder = library.folder(cfg) + "/"
    if file_hash and conn.execute("SELECT 1 FROM paths WHERE hash=? AND path LIKE ?",
                                  (file_hash, folder + "%")).fetchone():
        return True
    return bool(work_id) and library.copy_of(cfg, library.paths_of(conn, work_id)) is not None


def _mode(cfg: Config, rel: str, in_shared: bool) -> str:
    """How taking it in treats the original: "copy" from a shared folder or a
    managed one (canvas-sync re-downloads a moved file), else "move"."""
    return "copy" if in_shared or rename.blocked_reason(cfg, rel) else "move"


def listing(cfg: Config, conn: sqlite3.Connection, *, look: bool = True,
            all_works: dict | None = None) -> dict:
    """Papers in inbox folders and shared folders that the library lacks.

    `look=False` reads nothing (for the sidebar count): files not judged yet
    are counted in `unseen` instead.
    Each item: path, name, folder, source ("inbox" or "shared"), kind, why,
    work_id, title, suggested (collection), mode, in_library.
    """
    layout = contexts.layout_for(cfg)
    shared_map = shared.folders(cfg)
    all_works = works.load_all_readonly(cfg) if all_works is None else all_works
    items, unseen, looked = [], 0, 0

    # Preload library membership and indexed paths in two queries instead of N+1 per file.
    lib_prefix = library.folder(cfg) + "/"
    lib_hashes: set[str] = set()
    lib_works: set[str] = set()
    for row in conn.execute(
            "SELECT p.hash, f.work_id FROM paths p JOIN files f ON f.hash = p.hash WHERE p.path LIKE ?",
            (lib_prefix + "%",)):
        lib_hashes.add(row["hash"])
        if row["work_id"]:
            lib_works.add(row["work_id"])

    no_rename_lower = frozenset(n.lower() for n in cfg.no_rename)
    blocked_cache: dict[str, str | None] = {}

    def folder_mode(folder_rel: str, in_shared: bool) -> str:
        if in_shared:
            return "copy"
        if folder_rel not in blocked_cache:
            blocked_cache[folder_rel] = rename._folder_blocked(cfg, folder_rel, no_rename_lower)
        return "copy" if blocked_cache[folder_rel] else "move"

    def describe(rel: str, file_hash: str | None, work_id: str | None, kind: str, why: str,
                 source: str, suggested: str | None) -> dict:
        work = all_works.get(work_id or "") or {}
        meta = work.get("meta") or {}
        authors = [a.get("family") for a in meta.get("authors") or [] if a.get("family")]
        p = PurePosixPath(rel)
        folder_rel = str(p.parent)
        return {"path": rel, "name": p.name, "folder": folder_rel,
                "source": source, "kind": kind, "why": why, "work_id": work_id,
                "title": meta.get("title"), "authors": authors, "year": meta.get("year"),
                "suggested": suggested, "mode": folder_mode(folder_rel, source == "shared"),
                "in_library": (bool(file_hash) and file_hash in lib_hashes)
                              or (bool(work_id) and work_id in lib_works)}

    # 1. Shared folders: indexed, so the index says what each file is.
    for name, folder in shared_map.items():
        for row in conn.execute(
                "SELECT p.path, f.hash, f.work_id, f.kind, f.status FROM paths p JOIN files f ON f.hash = p.hash "
                "WHERE p.path LIKE ?", (folder + "/%",)):
            if row["work_id"] is None and row["kind"] not in PAPER_KINDS:
                continue
            items.append(describe(row["path"], row["hash"], row["work_id"], row["kind"] or "paper",
                                  "added to the shared folder", "shared", name))

    # 2. Inbox folders: not indexed (after the switch), so each file is hashed
    #    and judged once, then remembered while it is unchanged (downloads.py).
    if cfg.inboxes:
        indexed_paths = {
            row["path"]: (row["hash"], row["work_id"], row["kind"])
            for row in conn.execute("SELECT p.path, f.hash, f.work_id, f.kind FROM paths p JOIN files f ON f.hash = p.hash")
        }
        files_meta: dict[str, tuple[str | None, str | None, str | None]] | None = None
        for top in cfg.inboxes:
            for rel in _walk(cfg, top):
                if shared.collection_at(cfg, rel, shared_map):
                    continue                                 # listed above, with its collection
                known = indexed_paths.get(rel)
                if known is not None:
                    file_hash, work_id, kind = known
                    why = "indexed"
                    if work_id is None and kind not in PAPER_KINDS:
                        continue
                else:
                    path = cfg.absolute(rel)
                    try:
                        stat = path.stat()
                    except OSError:
                        continue
                    key = (str(path), stat.st_size, stat.st_mtime_ns)
                    if key not in downloads._seen:
                        if not look or looked >= LIMIT:
                            unseen += 1
                            continue
                        looked += 1
                    try:
                        file_hash, verdict = downloads._look(path, stat)
                    except OSError:
                        continue
                    # A file refs read before (when the folder was still indexed)
                    # keeps its work: the `files` row outlives the path.
                    if files_meta is None:
                        files_meta = {
                            r["hash"]: (r["work_id"], r["kind"], r["status"])
                            for r in conn.execute("SELECT hash, work_id, kind, status FROM files")
                        }
                    row = files_meta.get(file_hash)
                    work_id = row[0] if row else None
                    read = row is not None and row[2] is not None
                    kind, why = (row[1], "read before") if read and row[1] else (verdict.kind, verdict.why)
                    if work_id is None and kind not in {"paper", "maybe", *PAPER_KINDS}:
                        continue
                items.append(describe(rel, file_hash, work_id, kind or "paper", why, "inbox",
                                      collection.suggested(layout.derive(rel))))
    items.sort(key=lambda i: (i["source"] != "shared", i["folder"], i["name"]))
    return {"items": items, "unseen": unseen, "inboxes": list(cfg.inboxes), "shared": shared_map}


def waiting(cfg: Config, conn: sqlite3.Connection, *, all_works: dict | None = None) -> tuple[int, int]:
    """For the sidebar, reading nothing: (papers to take in, files not judged yet)."""
    found = listing(cfg, conn, look=False, all_works=all_works)
    return sum(not i["in_library"] for i in found["items"]), found["unseen"]


def take(cfg: Config, conn: sqlite3.Connection, choices: list[dict]) -> dict:
    """Bring PDFs into the library. `choices`: [{"path", "collections"}],
    paths from `listing`. Each is placed (library.place: canonical name when
    its record is trusted; moved, or copied from shared and managed folders)
    and journaled. Returns {"taken": [(path, new path, work id or None,
    collections)], "refused": [(path, why)]}. The caller holds the ingest
    lock, scans, puts known papers in their collections and reads new ones.
    """
    offered = {i["path"]: i for i in listing(cfg, conn)["items"]}
    all_works = works.load_all(cfg)
    taken, refused, names = [], [], set()
    for choice in choices:
        rel = choice.get("path") or ""
        item = offered.get(rel)
        if item is None:
            refused.append((rel, "not in the inbox (moved, or already in the library)"))
            continue
        try:
            new_rel = library.place(cfg, conn, rel, all_works.get(item["work_id"] or ""),
                                    mode=item["mode"], taken=names)
        except (library.Refused, OSError) as error:
            refused.append((rel, str(error)))
            continue
        names.add(PurePosixPath(new_rel).name)
        wanted = [n for n in choice.get("collections") or [] if n]
        taken.append((rel, new_rel, item["work_id"], wanted))
    return {"taken": taken, "refused": refused}


def folder_label(cfg: Config, rel: str) -> str:
    """A short name for where a PDF sits: its context ("coursework/2026-fall/220a")."""
    return contexts.layout_for(cfg).derive(rel).key

