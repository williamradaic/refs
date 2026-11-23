"""PDF highlights and margin notes: extract, index, and format your annotations.

`reading.py` counts how many pages you marked; this module extracts the actual
text you highlighted and the sticky/margin comments you wrote, caches them in
`index.db` (keyed by PDF content hash, so each version is read once), feeds
them into full-text and semantic search, and formats them for Obsidian notes.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING

from . import library, reading

if TYPE_CHECKING:
    from .config import Config

SCHEMA = """
CREATE TABLE IF NOT EXISTS highlights (
    hash    TEXT NOT NULL,
    idx     INTEGER NOT NULL,
    page    INTEGER NOT NULL,
    kind    TEXT NOT NULL,
    color   TEXT,
    quote   TEXT,
    comment TEXT,
    author  TEXT,
    date    TEXT,
    PRIMARY KEY (hash, idx)
);
CREATE INDEX IF NOT EXISTS highlights_hash ON highlights(hash);
CREATE TABLE IF NOT EXISTS highlights_done (
    hash TEXT PRIMARY KEY
);
"""

_TEXT_MARKUP = {"Highlight", "Underline", "StrikeOut", "Squiggly"}


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def _hex_color(rgb: list[float] | tuple[float, ...] | None) -> str | None:
    if not rgb or len(rgb) < 3:
        return None
    r, g, b = (max(0, min(255, round(float(c) * 255))) for c in rgb[:3])
    return f"#{r:02x}{g:02x}{b:02x}"


def _clean_quote(text: str) -> str:
    # Rejoin end-of-line hyphenation ("estima-\ntion" -> "estimation") and collapse whitespace.
    text = re.sub(r"(\w)-\s*\n\s*(\w)", r"\1\2", text)
    return re.sub(r"\s+", " ", text).strip()


def _quad_text(page, annot, pymupdf) -> str:
    """Extract the text covered by a text-markup annotation's quads (or rect)."""
    points = annot.vertices
    rects = []
    if points and len(points) >= 4:
        for i in range(0, len(points) - 3, 4):
            try:
                rects.append(pymupdf.Quad(points[i:i + 4]).rect)
            except Exception:  # noqa: BLE001
                continue
    if not rects:
        rects = [annot.rect]
    parts = []
    for rect in rects:
        if rect.is_empty or rect.is_infinite:
            continue
        chunk = page.get_text("text", clip=rect)
        if chunk and chunk.strip():
            parts.append(chunk)
    return _clean_quote(" ".join(parts))


def extract(path: Path, authors: set[str]) -> list[dict]:
    """Extract your highlights and margin comments from one PDF, in reading order."""
    mupdf = reading._mupdf()
    try:
        doc = mupdf.open(path)
    except Exception:  # noqa: BLE001
        return []
    found: list[tuple[tuple[int, float, float], dict]] = []
    try:
        for page in doc:
            for a in page.annots() or []:
                kind = a.type[1]
                if kind not in reading.MARKUP:
                    continue
                author = (a.info.get("title") or "").strip()
                if author.lower() not in authors:
                    continue
                comment = (a.info.get("content") or "").strip()
                quote = _quad_text(page, a, mupdf) if kind in _TEXT_MARKUP else ""
                if not quote and not comment:
                    continue
                colors = a.colors or {}
                color = _hex_color(colors.get("stroke") or colors.get("fill"))
                when = reading.pdf_date(a.info.get("modDate")) or reading.pdf_date(a.info.get("creationDate"))
                rect = a.rect
                found.append((
                    (page.number, round(rect.y0, 1), round(rect.x0, 1)),
                    {
                        "page": page.number + 1,
                        "kind": kind,
                        "color": color,
                        "quote": quote,
                        "comment": comment,
                        "author": author,
                        "date": when,
                    },
                ))
    except Exception:  # noqa: BLE001
        pass
    finally:
        doc.close()
    found.sort(key=lambda item: item[0])
    return [item for _, item in found]


def store(conn: sqlite3.Connection, file_hash: str, items: list[dict]) -> None:
    """Save extracted highlights for `file_hash` and mark it done."""
    ensure_schema(conn)
    conn.execute("DELETE FROM highlights WHERE hash=?", (file_hash,))
    if items:
        conn.executemany(
            "INSERT INTO highlights (hash, idx, page, kind, color, quote, comment, author, date) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            [(file_hash, i, h["page"], h["kind"], h["color"], h["quote"], h["comment"],
              h["author"], h["date"]) for i, h in enumerate(items)],
        )
    conn.execute("INSERT OR REPLACE INTO highlights_done (hash) VALUES (?)", (file_hash,))


def sync_for_hash(cfg: Config, conn: sqlite3.Connection, file_hash: str, rel_path: str) -> list[dict]:
    """Ensure highlights for `file_hash` are extracted and cached; return them."""
    ensure_schema(conn)
    done = conn.execute("SELECT 1 FROM highlights_done WHERE hash=?", (file_hash,)).fetchone()
    if done is None:
        authors = {name.strip().lower() for name in cfg.annotation_authors}
        items = extract(cfg.absolute(rel_path), authors)
        store(conn, file_hash, items)
        conn.commit()
        return items
    rows = conn.execute(
        "SELECT page, kind, color, quote, comment, author, date FROM highlights "
        "WHERE hash=? ORDER BY idx", (file_hash,),
    ).fetchall()
    return [dict(r) for r in rows]


def for_work(cfg: Config, conn: sqlite3.Connection, work_id: str) -> list[dict]:
    """Return your highlights and comments from the work's best marked PDF."""
    ensure_schema(conn)
    rows = conn.execute(
        "SELECT p.path, f.hash, f.marks FROM paths p JOIN files f ON f.hash = p.hash "
        "WHERE f.work_id = ? ORDER BY f.marks DESC, p.path", (work_id,),
    ).fetchall()
    if not rows:
        return []
    # Prefer the library copy when it has marks; otherwise whichever copy has the most marks.
    lib_prefix = library.folder(cfg) + "/"
    chosen = next((r for r in rows if r["path"].startswith(lib_prefix) and (r["marks"] or 0) > 0), None)
    if chosen is None:
        chosen = next((r for r in rows if (r["marks"] or 0) > 0), None)
    if chosen is None:
        return []
    return sync_for_hash(cfg, conn, chosen["hash"], chosen["path"])


def all_text_by_hash(conn: sqlite3.Connection) -> dict[str, str]:
    """Combined quotes and comments per file hash, for FTS5 and semantic search."""
    try:
        rows = conn.execute(
            "SELECT hash, quote, comment FROM highlights ORDER BY hash, idx"
        ).fetchall()
    except sqlite3.OperationalError:
        return {}
    buckets: dict[str, list[str]] = {}
    for row in rows:
        parts = [p for p in (row["quote"], row["comment"]) if p]
        if parts:
            buckets.setdefault(row["hash"], []).append(" ".join(parts))
    return {h: "\n".join(lines) for h, lines in buckets.items()}


def format_markdown(items: list[dict]) -> str:
    """Format extracted highlights as clean Markdown bullets with page numbers."""
    if not items:
        return ""
    lines = ["## PDF Highlights", ""]
    for item in items:
        page = item.get("page")
        quote = (item.get("quote") or "").strip()
        comment = (item.get("comment") or "").strip()
        prefix = f"- **p. {page}:** " if page else "- "
        if quote and comment:
            lines.append(f'{prefix}"{quote}" — *{comment}*')
        elif quote:
            lines.append(f'{prefix}"{quote}"')
        elif comment:
            lines.append(f"{prefix}*{comment}*")
    return "\n".join(lines)
