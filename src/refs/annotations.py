"""Annotations across a paper's copies: who marked which copy, and how recently.

A paper can sit in the library and, as a recorded copy, in a folder shared
with coauthors (shared.py). Each copy gathers its own highlights and notes:
yours in the library copy, a coauthor's in the shared one. The index keeps,
per file, everyone's marks by author and the newest mark's date (reading.survey,
counted with the scan). From that, each copy of a paper gets a line on the page
("library copy: your marks on 14 pages, last 12 Nov · bets copy: 6 marks by a
coauthor, last 18 Nov ⚑") and a flag when another copy has newer marks, or
marks the library copy lacks.

Read status already counts your marks on any copy (index._detect_reading).
Bringing the marks over is `pull` below.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from . import library, shared
from .config import Config


def copies(cfg: Config, conn: sqlite3.Connection, work_id: str) -> list[dict]:
    """Each file of a work on disk, library copy first: where it is, your
    marks, others' marks, the newest mark's date, and whether to flag it."""
    me = {name.strip().lower() for name in cfg.annotation_authors}
    shared_map = shared.folders(cfg)
    rows = conn.execute(
        "SELECT p.path, p.mtime, p.context, f.hash, f.marked_pages, f.marks, f.marks_by, f.marks_latest "
        "FROM paths p JOIN files f ON f.hash = p.hash WHERE f.work_id = ? ORDER BY p.path",
        (work_id,)).fetchall()
    found = []
    for row in rows:
        by = json.loads(row["marks_by"] or "{}")
        total = sum(by.values())
        # A mark with no date of its own: the file's own time stands in.
        latest = row["marks_latest"] or (
            datetime.fromtimestamp(row["mtime"], timezone.utc).isoformat(timespec="seconds")
            if total and row["mtime"] else None)
        in_shared = shared.collection_at(cfg, row["path"], shared_map)
        found.append({
            "path": row["path"], "hash": row["hash"],
            "where": "library" if library.holds(cfg, row["path"])
                     else f"#{in_shared}" if in_shared else (row["context"] or ""),
            "shared": in_shared,
            "your_pages": row["marked_pages"], "your_marks": row["marks"],
            "others": {a: n for a, n in by.items() if a.strip().lower() not in me},
            "by_author": by, "latest": latest if total else None,
            "flag": None,
        })
    found.sort(key=lambda c: c["where"] != "library")
    lib = next((c for c in found if c["where"] == "library"), None)
    if lib is None:
        return found
    for copy in found:
        if copy is lib or copy["hash"] == lib["hash"] or not copy["latest"]:
            continue
        lacking = [a for a, n in copy["by_author"].items() if n > lib["by_author"].get(a, 0)]
        newer = lib["latest"] is None or copy["latest"] > lib["latest"]
        if lacking:
            names = ", ".join(a or "no name" for a in lacking[:3])
            copy["flag"] = f"marks the library copy lacks ({names})"
        elif newer:
            copy["flag"] = "newer marks than the library copy"
    return found
