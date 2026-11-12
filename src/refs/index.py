"""The local index: a SQLite database that makes the library fast to query.

It is a cache, not the source of truth. It can be deleted at any time and
rebuilt with `refs scan`. Tables:

  files   one row per distinct PDF *content* (keyed by SHA-256 hash)
  paths   one row per location on disk; several paths can share one hash
  events  a log of what each scan noticed (added, moved, modified, ...)
  search  full-text index over titles, authors, file names and contexts
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import PurePosixPath

from . import contexts, naming
from .config import Config

# Bump when the tables change. An older index is simply dropped and rebuilt.
SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS files (
    hash        TEXT PRIMARY KEY,   -- SHA-256 of the content: the file's identity
    size        INTEGER NOT NULL,
    first_seen  TEXT NOT NULL,
    supersedes  TEXT,               -- hash of the earlier version, if edited in place
    work_id     TEXT,               -- filled in Phase 2 (link to a library record)
    kind        TEXT,               -- paper, slides, syllabus, problem-set, notes, ...
    kind_source TEXT,               -- 'path' = provisional guess from names and folders
    g_title     TEXT,               -- g_* = guesses read from the file name
    g_authors   TEXT,               -- JSON list of surnames
    g_year      INTEGER,
    g_ids       TEXT                -- JSON, e.g. {"nber": "32515"}
);

CREATE TABLE IF NOT EXISTS paths (
    path        TEXT PRIMARY KEY,   -- relative to the Dropbox root
    hash        TEXT NOT NULL REFERENCES files(hash),
    inode       INTEGER,
    mtime       REAL,
    size        INTEGER,
    last_seen   TEXT,
    context     TEXT,               -- e.g. coursework/2026-fall/220a
    area        TEXT,
    term        TEXT,
    slug        TEXT,
    section     TEXT,
    topic       TEXT,
    topic_order INTEGER
);
CREATE INDEX IF NOT EXISTS paths_hash ON paths(hash);

CREATE TABLE IF NOT EXISTS events (
    id       INTEGER PRIMARY KEY,
    ts       TEXT NOT NULL,
    kind     TEXT NOT NULL,         -- added, copy, moved, modified, removed, restored
    path     TEXT NOT NULL,
    old_path TEXT,
    hash     TEXT
);

CREATE VIRTUAL TABLE IF NOT EXISTS search USING fts5(
    hash UNINDEXED, title, authors, aliases, filename, context,
    tokenize = 'unicode61 remove_diacritics 2'
);
"""


def connect(cfg: Config) -> sqlite3.Connection:
    """Open (and if needed create) the index."""
    cfg.index_db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(cfg.index_db, timeout=30)
    conn.row_factory = sqlite3.Row            # rows behave like dictionaries
    conn.execute("PRAGMA journal_mode = WAL")  # lets a reader and a writer coexist

    # An index from an older version of the program is thrown away, not migrated:
    # everything in it can be recomputed from the files on disk.
    has_meta = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name = 'meta'"
    ).fetchone()
    if has_meta:
        row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
        if row is None or int(row["value"]) != SCHEMA_VERSION:
            for table in ("search", "events", "paths", "files", "meta"):
                conn.execute(f"DROP TABLE IF EXISTS {table}")

    conn.executescript(SCHEMA)
    conn.execute(
        "INSERT OR REPLACE INTO meta VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),)
    )
    conn.commit()
    return conn


# --- Provisional document kind ------------------------------------------------

def guess_kind(rel: str, ctx: contexts.Context, guess: naming.NameGuess) -> str:
    """First, cheap guess at what a PDF is, from its name and folder alone.

    Phase 2 replaces this with a classification that actually reads the PDF.
    """
    path = PurePosixPath(rel)
    name = path.name.lower()
    folders = "/".join(path.parts[:-1]).lower()
    title_words = len((guess.title or "").split())

    # A bare NBER/SSRN/arXiv id, or a name in the house convention with a real
    # title, is a paper whatever words that title contains ("...-nobel-lecture").
    if guess.style in {"jstor", "id"}:
        return "paper"
    if guess.style == "convention" and ((guess.authors and title_words >= 2) or title_words >= 3):
        return "paper"

    # Otherwise look for tell-tale words, first in the file name, then in the
    # folders above it ("Problem Sets/coding/plot.pdf").
    for text in (name, folders):
        if re.search(r"syllab|reading[ _-]?list", text):
            return "syllabus"
        if text is folders and "reading" in text:
            break       # inside a readings folder: leave it to the rules below
        if re.search(r"problem[ _-]?set|pset|(?<![a-z])ps[ _-]?\d|homework|(?<![a-z])hw[ _-]?\d"
                     r"|solutions?(?![a-z])|exams?(?![a-z])|midterm|(?<![a-z])final(?![a-z ]| paper)", text):
            return "problem-set"
        if "notes" in text:
            return "notes"
        if re.search(r"slides?(?![a-z])|lecture|handout|(?<![a-z])week[ _-]?\d|class[ _-]?\d"
                     r"|(?<![a-z])sections?(?![a-z])", text):
            return "slides"

    if ctx.section == "canvas-sync":
        return "course-material"
    # Anything inside a readings/<topic>/ folder, or named like "Author 2017".
    if ctx.topic or guess.looks_like_paper:
        return "paper"
    return "unknown"


# --- Derived columns and the search index -------------------------------------

_STYLE_RANK = {"convention": 0, "jstor": 1, "loose": 2, "id": 3, "none": 4}


def refresh_derived(cfg: Config, conn: sqlite3.Connection) -> None:
    """Recompute everything that follows from paths alone.

    Run after every scan. Contexts, name guesses, provisional kinds and the
    search index are all functions of the current paths, so they are rebuilt
    wholesale. With a few thousand files this takes well under a second.
    """
    by_hash: dict[str, list[tuple[str, contexts.Context]]] = {}

    # 1. Context columns for every path.
    for row in conn.execute("SELECT path, hash FROM paths").fetchall():
        ctx = contexts.derive(row["path"], cfg.dump)
        conn.execute(
            "UPDATE paths SET context=?, area=?, term=?, slug=?, section=?, topic=?, "
            "topic_order=? WHERE path=?",
            (ctx.key, ctx.area, ctx.term, ctx.slug, ctx.section, ctx.topic,
             ctx.topic_order, row["path"]),
        )
        by_hash.setdefault(row["hash"], []).append((row["path"], ctx))

    # 2. Per file: the most informative name among its paths, and its kind.
    conn.execute("DELETE FROM search")
    for file_hash, located in by_hash.items():
        guesses = [(naming.parse_filename(PurePosixPath(p).name), p, ctx) for p, ctx in located]
        guesses.sort(key=lambda item: _STYLE_RANK[item[0].style])
        best, best_path, best_ctx = guesses[0]

        # Merge identifiers from all names (one copy may be "w32515.pdf").
        ids: dict[str, str] = {}
        for guess, _, _ in guesses:
            ids.update(guess.ids)

        # Prefer the context that carries a topic when judging the kind.
        kind_ctx = next((ctx for _, _, ctx in guesses if ctx.topic), best_ctx)
        conn.execute(
            "UPDATE files SET g_title=?, g_authors=?, g_year=?, g_ids=?, "
            "kind = CASE WHEN kind_source IS NULL OR kind_source='path' THEN ? ELSE kind END, "
            "kind_source = COALESCE(kind_source, 'path') WHERE hash=?",
            (best.title, json.dumps(best.authors), best.year, json.dumps(ids),
             guess_kind(best_path, kind_ctx, best), file_hash),
        )

        # 3. One search row per file. `aliases` holds author initials, so that
        #    "blp" finds Berry-Levinsohn-Pakes.
        initials = "".join(a[0] for a in best.authors if a) if len(best.authors) >= 2 else ""
        conn.execute(
            "INSERT INTO search (hash, title, authors, aliases, filename, context) "
            "VALUES (?,?,?,?,?,?)",
            (
                file_hash,
                best.title or "",
                " ".join(best.authors),
                " ".join(filter(None, [initials, *ids.values()])),
                " ".join(PurePosixPath(p).stem for p, _ in located),
                " ".join(f"{ctx.key} {ctx.topic or ''} {ctx.section or ''}" for _, ctx in located),
            ),
        )


# --- Queries --------------------------------------------------------------------

# `key:value` filters accepted by search(), mapped to what they compare against.
FILTER_ALIASES = {"course": "slug", "project": "slug", "in": "context"}
PATH_FILTERS = {"slug", "term", "area", "context", "topic", "section"}
FILE_FILTERS = {"kind", "year", "author"}


def _file_dict(row: sqlite3.Row, locations: list[sqlite3.Row]) -> dict:
    """Shape one file and its locations for display or JSON output."""
    return {
        "hash": row["hash"],
        "title": row["g_title"],
        "authors": json.loads(row["g_authors"] or "[]"),
        "year": row["g_year"],
        "ids": json.loads(row["g_ids"] or "{}"),
        "kind": row["kind"],
        "kind_source": row["kind_source"],
        "locations": [
            {
                "path": loc["path"], "context": loc["context"], "area": loc["area"],
                "term": loc["term"], "slug": loc["slug"], "section": loc["section"],
                "topic": loc["topic"], "topic_order": loc["topic_order"],
            }
            for loc in locations
        ],
    }


def search(
    conn: sqlite3.Connection, terms: list[str], filters: dict[str, str] | None = None,
    limit: int | None = 50,
) -> list[dict]:
    """Find files by free text and/or `key:value` filters.

    Free-text words must all match (prefix match, so "demand est" works).
    A file passes a path filter if *any* of its locations satisfies it.
    """
    filters = {FILTER_ALIASES.get(k, k): v.lower() for k, v in (filters or {}).items()}
    unknown = set(filters) - PATH_FILTERS - FILE_FILTERS
    if unknown:
        raise ValueError(f"unknown filter(s): {', '.join(sorted(unknown))}")

    # Candidate files, best text match first (or all files when there is no text).
    if terms:
        # Each word is quoted (so punctuation is harmless) and prefix-matched.
        match = " AND ".join('"' + t.replace('"', '""') + '"*' for t in terms)
        rows = conn.execute(
            "SELECT f.* FROM search JOIN files f ON f.hash = search.hash "
            "WHERE search MATCH ? ORDER BY bm25(search)", (match,),
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM files").fetchall()

    locations: dict[str, list[sqlite3.Row]] = {}
    for loc in conn.execute("SELECT * FROM paths ORDER BY path"):
        locations.setdefault(loc["hash"], []).append(loc)

    def passes(row: sqlite3.Row, locs: list[sqlite3.Row]) -> bool:
        for key, wanted in filters.items():
            if key == "kind" and (row["kind"] or "") != wanted:
                return False
            if key == "year" and str(row["g_year"] or "") != wanted:
                return False
            if key == "author" and wanted not in (row["g_authors"] or "").lower():
                return False
            if key in PATH_FILTERS:
                def hit(loc: sqlite3.Row) -> bool:
                    value = (loc[key] or "").lower()
                    # context and topic match loosely; the rest must be exact.
                    return wanted in value if key in {"context", "topic"} else value == wanted
                if not any(hit(loc) for loc in locs):
                    return False
        return True

    results = [
        _file_dict(row, locations[row["hash"]])
        for row in rows
        if row["hash"] in locations and passes(row, locations[row["hash"]])
    ]

    if not terms:   # no relevance ranking available: order like the folder tree
        results.sort(key=lambda r: (
            r["locations"][0]["context"] or "",
            r["locations"][0]["topic_order"] if r["locations"][0]["topic_order"] is not None else 999,
            r["locations"][0]["path"],
        ))
    return results[:limit] if limit else results
