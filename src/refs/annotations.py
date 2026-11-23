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
Bringing the marks over is `pull` below (the pull merge).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import PurePosixPath

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


# --- Bringing marks over: the pull merge ----------------------------------------------------
# "Bring annotations from the bets copy": each mark in that copy that the
# library copy lacks is drawn again in the library copy, with its author,
# text, colour and dates. Matching: the mark's own id (/NM) with its page,
# kind and author, else page + kind + author + position. Only the library copy is written, after a copy
# of it is kept in refs' folder. Deletions do not travel (a mark removed in
# one copy is not removed in the other), and a redrawn mark may look slightly
# different from the original. GoodNotes or Notability ink that never reached
# the PDF cannot be seen.

QUIET_SECONDS = 300          # a file changed this recently may still be syncing


class Refused(Exception):
    """A merge refs will not do; the message says why."""


def _marks(doc) -> list:
    """(page, mark) pairs. The page objects are kept with their marks: a mark
    holds only a weak link to its page and comes loose if the page is freed."""
    from .reading import MARKUP
    pages = list(doc)
    return [(page, a) for page in pages for a in (page.annots() or []) if a.type[1] in MARKUP]


def _key(page_number: int, annot) -> tuple:
    """A mark without a usable id: page, kind, author and position (two people
    highlighting the same passage made two marks)."""
    rect = annot.rect
    return (page_number, annot.type[1], (annot.info.get("title") or "").strip().lower(),
            round(rect.x0), round(rect.y0), round(rect.x1), round(rect.y1))


def _redraw(page, annot):
    """Draw one mark again on `page` (another document). None if its kind
    cannot be redrawn."""
    import pymupdf
    kind, points = annot.type[1], annot.vertices
    if kind in {"Highlight", "Underline", "StrikeOut", "Squiggly"}:
        quads = [pymupdf.Quad(points[i:i + 4]) for i in range(0, len(points or []) - 3, 4)] or [annot.rect]
        draw = {"Highlight": page.add_highlight_annot, "Underline": page.add_underline_annot,
                "StrikeOut": page.add_strikeout_annot, "Squiggly": page.add_squiggly_annot}[kind]
        return draw(quads=quads)
    if kind == "Text":
        return page.add_text_annot(annot.rect.tl, annot.info.get("content") or "",
                                   icon=annot.info.get("name") or "Note")
    if kind == "FreeText":
        return page.add_freetext_annot(annot.rect, annot.info.get("content") or "")
    if kind == "Ink" and points:
        return page.add_ink_annot(points)
    if kind == "Square":
        return page.add_rect_annot(annot.rect)
    if kind == "Circle":
        return page.add_circle_annot(annot.rect)
    if kind == "Line" and points and len(points) >= 2:
        return page.add_line_annot(points[0], points[1])
    if kind == "Polygon" and points:
        return page.add_polygon_annot(points)
    if kind == "PolyLine" and points:
        return page.add_polyline_annot(points)
    if kind == "Caret":
        return page.add_caret_annot(annot.rect.tl)
    return None


def _copy_look(doc, new, annot) -> None:
    """Author, text, dates, colour, opacity, line width and id, as the original."""
    import pymupdf
    info = annot.info
    new.set_info(title=info.get("title") or "", content=info.get("content") or "",
                 subject=info.get("subject") or "", creationDate=info.get("creationDate") or None,
                 modDate=info.get("modDate") or None)
    colors = annot.colors or {}
    try:
        new.set_colors(stroke=colors.get("stroke") or None, fill=colors.get("fill") or None)
    except Exception:                                    # noqa: BLE001 (a kind without a fill)
        new.set_colors(stroke=colors.get("stroke") or None)
    if annot.opacity is not None and 0 <= annot.opacity < 1:
        new.set_opacity(annot.opacity)
    if (width := (annot.border or {}).get("width")) and width > 0:
        try:
            new.set_border(width=width)
        except Exception:                                # noqa: BLE001
            pass
    if info.get("id"):                                   # so the next pull recognises it
        doc.xref_set_key(new.xref, "NM", pymupdf.get_pdf_str(info["id"]))
    new.update()


def pull(cfg: Config, conn: sqlite3.Connection, work_id: str, source: str, *, into: str | None = None,
         now: float | None = None) -> dict:
    """Bring the marks of one copy of a paper (`source`, a path) into its
    library copy, or into another copy of it (`into`: the one the Copies
    review keeps). Returns {"added", "skipped" (kinds that cannot be redrawn),
    "backup" (the library copy as it was, under refs' folder) or None}.
    Refused when there is no library copy, when the two are different
    versions (their pages do not line up), or when either changed in the last
    few minutes (Dropbox may still be syncing: a write now could make a
    conflicted copy)."""
    import shutil
    import time

    import pymupdf

    paths = library.paths_of(conn, work_id)
    target = into or library.copy_of(cfg, paths)
    if target is None:
        raise Refused("the paper has no copy in the library folder to bring marks into")
    if target not in paths:
        raise Refused("that is not a copy of this paper")
    if source not in paths or source == target:
        raise Refused("that is not another copy of this paper")
    now = time.time() if now is None else now
    for rel in (source, target):
        if now - cfg.absolute(rel).stat().st_mtime < QUIET_SECONDS:
            raise Refused(f"{PurePosixPath(rel).name} changed in the last few minutes; try again once "
                          "Dropbox has finished syncing it")
    src, dst = pymupdf.open(cfg.absolute(source)), pymupdf.open(cfg.absolute(target))
    try:
        if src.page_count != dst.page_count:
            raise Refused(f"the copies are different versions ({src.page_count} and {dst.page_count} pages); "
                          "their marks cannot be lined up")
        # An id alone is not enough: some programs number marks per file
        # ("fitz-A0"), so two copies can share one for different marks. With
        # its page, kind and author it is.
        def ident(page, annot) -> tuple | None:
            mark_id = annot.info.get("id")
            return (page.number, annot.type[1], (annot.info.get("title") or "").strip().lower(), mark_id) \
                if mark_id else None

        existing = _marks(dst)
        have_ids = {ident(p, a) for p, a in existing} - {None}
        have_keys = {_key(p.number, a) for p, a in existing}
        targets = list(dst)                              # kept alive while marks are drawn on them
        added = skipped = 0
        for page, annot in _marks(src):
            if ident(page, annot) in have_ids or _key(page.number, annot) in have_keys:
                continue
            new = _redraw(targets[page.number], annot)
            if new is None:
                skipped += 1
                continue
            _copy_look(dst, new, annot)
            added += 1
        if not added:
            return {"added": 0, "skipped": skipped, "backup": None}
        # Keep the library copy as it was, then write it in place.
        from . import works
        work = works.load_one(cfg, work_id) or {}
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = f"{cfg.state_dir}/annotation-backups/{work.get('citekey') or work_id}-{stamp}.pdf"
        cfg.absolute(backup).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(cfg.absolute(target), cfg.absolute(backup))
        if dst.can_save_incrementally():
            dst.saveIncr()                               # appends: the original bytes stay intact
        else:
            temporary = cfg.absolute(target).with_suffix(".refs-tmp")
            dst.save(temporary, garbage=1, deflate=True)
            dst.close()
            temporary.replace(cfg.absolute(target))
        return {"added": added, "skipped": skipped, "backup": backup}
    finally:
        src.close()
        if not dst.is_closed:
            dst.close()


def works_of(cfg: Config) -> dict:
    from . import works
    return works.load_all(cfg)
