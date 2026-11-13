"""Reconcile the index with what is actually on disk.

One scan answers, for every PDF: is it new, a copy of something known, the
same file under a new name or folder, an edited version, or gone?

Identity rules, in order:
  1. Same path, same size and modified-time  -> unchanged (no need to re-read it).
  2. A new path whose inode (the disk's own file ID) matches a path that just
     vanished                                -> moved or renamed.
  3. A new path whose content hash is known  -> moved (if the old path vanished)
                                                or a copy (if it is still there).
  4. Same path, different content            -> modified; the new version
                                                remembers which one it replaced.
"""

from __future__ import annotations

import sqlite3
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

from . import embed, index, storage
from .config import Config


@dataclass
class ScanReport:
    """What one scan found. Lists hold Dropbox-relative paths."""

    seen: int = 0
    unchanged: int = 0
    added: list[str] = field(default_factory=list)       # never seen before
    restored: list[str] = field(default_factory=list)    # known content, back on disk
    copies: list[str] = field(default_factory=list)      # extra location of a known file
    moved: list[tuple[str, str]] = field(default_factory=list)   # (old path, new path)
    modified: list[str] = field(default_factory=list)    # content changed in place
    removed: list[str] = field(default_factory=list)
    online_only: list[str] = field(default_factory=list)  # not downloaded; skipped
    seconds: float = 0.0
    embedded: int = 0                                     # texts given new vectors
    embed_error: str | None = None                        # why embedding failed, if it did

    @property
    def changes(self) -> int:
        return (len(self.added) + len(self.restored) + len(self.copies)
                + len(self.moved) + len(self.modified) + len(self.removed))


def _ensure_file(conn: sqlite3.Connection, file_hash: str, size: int, now: str,
                 supersedes: str | None = None) -> bool:
    """Make sure `files` has a row for this content. Returns True if it was new."""
    exists = conn.execute("SELECT 1 FROM files WHERE hash=?", (file_hash,)).fetchone()
    if exists:
        return False
    conn.execute(
        "INSERT INTO files (hash, size, first_seen, supersedes) VALUES (?,?,?,?)",
        (file_hash, size, now, supersedes),
    )
    if supersedes:
        # An edited file (say, annotated in Preview) is the same document: carry
        # over what was known about the previous version.
        conn.execute(
            "UPDATE files SET work_id=o.work_id, kind=o.kind, kind_source=o.kind_source "
            "FROM (SELECT work_id, kind, kind_source FROM files WHERE hash=?) AS o "
            "WHERE files.hash=?",
            (supersedes, file_hash),
        )
    return True


def _put_path(conn: sqlite3.Connection, disk: storage.DiskFile, file_hash: str, now: str) -> None:
    """Insert or update the row for one location. Context columns are filled later."""
    conn.execute(
        "INSERT INTO paths (path, hash, inode, mtime, size, last_seen) VALUES (?,?,?,?,?,?) "
        "ON CONFLICT(path) DO UPDATE SET hash=excluded.hash, inode=excluded.inode, "
        "mtime=excluded.mtime, size=excluded.size, last_seen=excluded.last_seen",
        (disk.rel, file_hash, disk.inode, disk.mtime, disk.size, now),
    )


def _log(conn: sqlite3.Connection, now: str, kind: str, path: str,
         file_hash: str | None, old_path: str | None = None) -> None:
    conn.execute(
        "INSERT INTO events (ts, kind, path, old_path, hash) VALUES (?,?,?,?,?)",
        (now, kind, path, old_path, file_hash),
    )


def scan(cfg: Config, conn: sqlite3.Connection,
         progress: Callable[[int, int], None] | None = None) -> ScanReport:
    """Bring the index in line with the disk and report what changed.

    Read-only with respect to your files: it only reads PDFs and writes the index.
    `progress(done, total)` is called while files are being hashed.
    """
    started = time.monotonic()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    report = ScanReport()

    # --- Snapshot both sides -------------------------------------------------
    disk = {f.rel: f for f in storage.walk_pdfs(cfg)}
    known = {row["path"]: row for row in conn.execute("SELECT * FROM paths")}
    report.seen = len(disk)

    # Paths in the index that are no longer on disk: either deleted, or the
    # "from" side of a move. Indexed two ways so a new path can claim one.
    gone = set(known) - set(disk)
    gone_by_inode = {(known[p]["inode"], known[p]["size"], known[p]["mtime"]): p for p in gone}
    gone_by_hash: dict[str, list[str]] = defaultdict(list)
    for p in gone:
        gone_by_hash[known[p]["hash"]].append(p)
    claimed: set[str] = set()   # vanished paths already matched to a new path

    # Only files that are new or changed need their content read.
    to_hash = sum(
        1 for rel, f in disk.items()
        if not f.online_only and (
            rel not in known
            or known[rel]["mtime"] != f.mtime or known[rel]["size"] != f.size
        )
    )
    hashed = 0

    def content_hash(rel: str) -> str:
        nonlocal hashed
        digest = storage.sha256(cfg, rel)
        hashed += 1
        if progress:
            progress(hashed, to_hash)
        return digest

    # --- Walk the disk -------------------------------------------------------
    for rel, f in disk.items():
        old = known.get(rel)

        # Case A: a path the index already knows.
        if old is not None:
            if (old["mtime"] == f.mtime and old["size"] == f.size) or f.online_only:
                report.unchanged += 1
                continue
            new_hash = content_hash(rel)
            if new_hash != old["hash"]:
                _ensure_file(conn, new_hash, f.size, now, supersedes=old["hash"])
                _log(conn, now, "modified", rel, new_hash)
                report.modified.append(rel)
            else:
                report.unchanged += 1        # touched, but same content
            _put_path(conn, f, new_hash, now)
            continue

        # Case B: a path the index has not seen.
        if f.online_only:                    # reading it would force a download
            report.online_only.append(rel)
            continue

        # B1. Same inode, size and time as a vanished path: a rename or move.
        #     The content cannot have changed, so there is no need to re-read it.
        old_path = gone_by_inode.get((f.inode, f.size, f.mtime))
        if old_path is not None and old_path not in claimed:
            file_hash = known[old_path]["hash"]
        else:
            # B2. Otherwise read the content and look the hash up.
            file_hash = content_hash(rel)
            candidates = [p for p in gone_by_hash.get(file_hash, []) if p not in claimed]
            old_path = candidates[0] if candidates else None

        if old_path is not None:
            claimed.add(old_path)
            conn.execute("DELETE FROM paths WHERE path=?", (old_path,))
            _put_path(conn, f, file_hash, now)
            _log(conn, now, "moved", rel, file_hash, old_path)
            report.moved.append((old_path, rel))
            continue

        # B3. Not a move. New content, a copy, or known content coming back.
        is_new_content = _ensure_file(conn, file_hash, f.size, now)
        other_locations = [
            row["path"] for row in
            conn.execute("SELECT path FROM paths WHERE hash=?", (file_hash,))
            if row["path"] not in gone
        ]
        _put_path(conn, f, file_hash, now)
        if is_new_content:
            kind, bucket = "added", report.added
        elif other_locations:
            kind, bucket = "copy", report.copies
        else:
            kind, bucket = "restored", report.restored
        _log(conn, now, kind, rel, file_hash)
        bucket.append(rel)

    # --- Whatever vanished and was not claimed by a move is gone -------------
    # The `files` row stays: if the same content reappears it is recognised.
    for p in sorted(gone - claimed):
        conn.execute("DELETE FROM paths WHERE path=?", (p,))
        _log(conn, now, "removed", p, known[p]["hash"])
        report.removed.append(p)

    # --- Recompute contexts, name guesses, kinds and the search index --------
    index.refresh_derived(cfg, conn)
    conn.execute("INSERT OR REPLACE INTO meta VALUES ('last_scan', ?)", (now,))
    conn.commit()

    # --- Vectors for semantic search: only what is new or changed -------------
    # A failure here (say, no network for the first model download) must not
    # spoil the scan: keyword search works without vectors.
    try:
        report.embedded = embed.refresh(cfg, conn)
        conn.commit()
    except Exception as error:                       # noqa: BLE001
        conn.rollback()
        report.embed_error = f"{type(error).__name__}: {error}"

    report.seconds = time.monotonic() - started
    return report
