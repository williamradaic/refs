"""The local index: a SQLite database that makes the library fast to query.

It is a cache, not the source of truth. It can be deleted at any time and
rebuilt with `refs scan`. Tables:

  files   one row per distinct PDF *content* (keyed by SHA-256 hash)
  paths   one row per location on disk; several paths can share one hash
  events  a log of what each scan noticed (added, moved, modified, ...)
  search  full-text index over titles, authors, file names, contexts and notes
  vectors one vector per item (and per piece of its note) for semantic search;
          see embed.py
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import PurePosixPath

from . import contexts, embed, naming, notes, query, records, works
from .config import Config

# Bump when the tables change. An older index is simply dropped and rebuilt.
SCHEMA_VERSION = 3

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS files (
    hash        TEXT PRIMARY KEY,   -- SHA-256 of the content: the file's identity
    size        INTEGER NOT NULL,
    first_seen  TEXT NOT NULL,
    supersedes  TEXT,               -- hash of the earlier version, if edited in place
    work_id     TEXT,               -- the work (paper) this file is a copy or version of
    citekey     TEXT,
    kind        TEXT,               -- paper, slides, syllabus, problem-set, notes, ...
    kind_source TEXT,               -- 'path' = guess from names/folders; 'read' = pipeline
    g_title     TEXT,               -- title, authors, year: from the read record when the
    g_authors   TEXT,               --   file has been processed, else guessed from its name
    g_year      INTEGER,            --   (g_authors is a JSON list of surnames)
    g_ids       TEXT,               -- JSON, e.g. {"nber": "32515"}
    status      TEXT,               -- verified, extracted, needs-review, classified, ...
    venue       TEXT,
    doi         TEXT,
    description TEXT,
    tags        TEXT                -- JSON list
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
    hash UNINDEXED, title, authors, aliases, filename, context, venue, tags, description, notes,
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

    # The search table gained a `notes` column. Only that table is rebuilt (from
    # the rows already in the index), not the whole index: a full rebuild
    # would mean reading every PDF in Dropbox again.
    old_search = [r["name"] for r in conn.execute("PRAGMA table_info(search)")]
    rebuild_search = bool(old_search) and "notes" not in old_search
    if rebuild_search:
        conn.execute("DROP TABLE search")

    conn.executescript(SCHEMA)
    embed.ensure_schema(conn)
    conn.execute(
        "INSERT OR REPLACE INTO meta VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),)
    )
    if rebuild_search:
        refresh_derived(cfg, conn)
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

    # 2. Per file: metadata from its read record if the pipeline has processed it,
    #    otherwise the most informative file name among its paths.
    supersedes = dict(conn.execute("SELECT hash, supersedes FROM files").fetchall())
    file_to_work = works.sync(cfg)          # also writes new or changed work files
    all_works = works.load_all(cfg)
    note_text = notes.bodies(cfg)           # your reading notes, by citekey
    conn.execute("DELETE FROM search")
    for file_hash, located in by_hash.items():
        guesses = [(naming.parse_filename(PurePosixPath(p).name), p, ctx) for p, ctx in located]
        guesses.sort(key=lambda item: _STYLE_RANK[item[0].style])
        best, best_path, best_ctx = guesses[0]

        # Merge identifiers from all names (one copy may be "w32515.pdf").
        ids: dict[str, str] = {}
        for guess, _, _ in guesses:
            ids.update(guess.ids)

        # Defaults: guessed from names and folders.
        kind_ctx = next((ctx for _, _, ctx in guesses if ctx.topic), best_ctx)
        title, authors, year = best.title, best.authors, best.year
        kind, kind_source = guess_kind(best_path, kind_ctx, best), "path"
        status = venue = doi = description = None
        tags: list[str] = []

        # An edited file (annotated, say) has a new hash but is the same document:
        # fall back to the read record of the version it replaced.
        read = records.load_read(cfg, file_hash)
        if read is None and supersedes.get(file_hash):
            read = records.load_read(cfg, supersedes[file_hash])
        if read is not None:
            status = read["status"]
            if registry := read.get("registry"):       # confidential files: ids only
                ids.update({"doi": registry["record"]["doi"]} if registry["record"].get("doi") else {})
            if reading := read.get("reading"):
                meta = records.final_metadata(read)
                title = meta["title"] or title
                authors = meta["surnames"] or authors
                year = meta["year"] or year
                venue, doi = meta["venue"], meta["doi"]
                kind, kind_source = reading["kind"], "read"
                description = reading.get("description")
                tags = [*reading.get("fields", []), *reading.get("methods", []), *reading.get("topics", [])]

        # A file that belongs to a work shows the work's metadata: a working
        # paper filed under its published version is listed as that publication.
        work_id = file_to_work.get(file_hash) or file_to_work.get(supersedes.get(file_hash) or "")
        citekey = None
        former_names: list[str] = []
        if work_id and "meta" in all_works.get(work_id, {}):
            work = all_works[work_id]
            meta, citekey = work["meta"], work["citekey"]
            title, year = meta.get("title") or title, meta.get("year") or year
            authors = [a["family"] for a in meta.get("authors") or [] if a.get("family")] or authors
            venue, doi, status = meta.get("venue"), meta.get("doi"), meta.get("status")
            tags = list(dict.fromkeys([*tags, *work["user"].get("tags", []),
                                       *work["user"].get("collections", [])]))   # no repeats, order kept
            ids.update({f"alias{n}": a for n, a in enumerate(
                [*work.get("aliases", []), *work.get("bib_keys", {})])})
            former_names = work["user"].get("former_names", [])

        conn.execute(
            "UPDATE files SET g_title=?, g_authors=?, g_year=?, g_ids=?, kind=?, kind_source=?, "
            "status=?, venue=?, doi=?, description=?, tags=?, work_id=?, citekey=? WHERE hash=?",
            (title, json.dumps(authors, ensure_ascii=False), year, json.dumps(ids), kind,
             kind_source, status, venue, doi, description,
             json.dumps(tags, ensure_ascii=False), work_id, citekey, file_hash),
        )

        # 3. One search row per file. `aliases` holds author initials, so that
        #    "blp" finds Berry-Levinsohn-Pakes.
        initials = "".join(a[0] for a in authors if a) if len(authors) >= 2 else ""
        conn.execute(
            "INSERT INTO search (hash, title, authors, aliases, filename, context, venue, tags, "
            "description, notes) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                file_hash,
                title or "",
                " ".join(authors),
                " ".join(filter(None, [initials, citekey, *ids.values()])),
                " ".join([*(PurePosixPath(p).stem for p, _ in located),
                          *(PurePosixPath(n).stem for n in former_names)]),
                " ".join(f"{ctx.key} {ctx.topic or ''} {ctx.section or ''}" for _, ctx in located),
                venue or "",
                " ".join(tags),
                description or "",
                note_text.get(citekey or "", ""),
            ),
        )

    # 4. Works with no PDF on disk (quick-added without a file, or imported from
    #    Zotero) get a stand-in row, so they can be found like any other.
    conn.execute("DELETE FROM files WHERE hash LIKE 'work:%'")
    with_files = {file_to_work[h] for h in by_hash if h in file_to_work}
    for work_id, work in all_works.items():
        if work_id in with_files or "meta" not in work:
            continue
        meta, user = work["meta"], work["user"]
        authors = [a["family"] for a in meta.get("authors") or [] if a.get("family")]
        tags = list(dict.fromkeys([*work.get("tags", []), *user.get("tags", []),
                                   *user.get("collections", [])]))
        conn.execute(
            "INSERT INTO files (hash, size, first_seen, work_id, citekey, kind, kind_source, "
            "g_title, g_authors, g_year, g_ids, status, venue, doi, description, tags) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (f"work:{work_id}", 0, work["created"], work_id, work["citekey"], meta.get("kind"),
             "work", meta.get("title"), json.dumps(authors, ensure_ascii=False), meta.get("year"),
             "{}", "no-file", meta.get("venue"), meta.get("doi"), work.get("description"),
             json.dumps(tags, ensure_ascii=False)),
        )
        initials = "".join(a[0] for a in authors if a) if len(authors) >= 2 else ""
        conn.execute(
            "INSERT INTO search (hash, title, authors, aliases, filename, context, venue, tags, "
            "description, notes) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (f"work:{work_id}", meta.get("title") or "", " ".join(authors),
             " ".join(filter(None, [initials, work["citekey"], *work.get("aliases", []),
                                    *work.get("bib_keys", {})])),
             "", "", meta.get("venue") or "", " ".join(tags), work.get("description") or "",
             note_text.get(work["citekey"], "")),
        )


# --- Queries --------------------------------------------------------------------

# `key:value` filters accepted by search(), mapped to what they compare against.
FILTER_ALIASES = {"course": "slug", "project": "slug", "in": "context"}
PATH_FILTERS = {"slug", "term", "area", "context", "topic", "section"}
FILE_FILTERS = {"kind", "year", "author", "status", "tag"}


def _file_dict(row: sqlite3.Row, locations: list[sqlite3.Row]) -> dict:
    """Shape one file and its locations for display or JSON output."""
    return {
        "hash": row["hash"],
        "work_id": row["work_id"],
        "citekey": row["citekey"],
        "title": row["g_title"],
        "authors": json.loads(row["g_authors"] or "[]"),
        "year": row["g_year"],
        "ids": json.loads(row["g_ids"] or "{}"),
        "kind": row["kind"],
        "kind_source": row["kind_source"],
        "status": row["status"],
        "venue": row["venue"],
        "doi": row["doi"],
        "description": row["description"],
        "tags": json.loads(row["tags"] or "[]"),
        "locations": [
            {
                "path": loc["path"], "context": loc["context"], "area": loc["area"],
                "term": loc["term"], "slug": loc["slug"], "section": loc["section"],
                "topic": loc["topic"], "topic_order": loc["topic_order"],
            }
            for loc in locations
        ],
    }


# --- Semantic ranking settings (embed.py) ---------------------------------------
# Only items at least this similar to the query count as semantic matches, and
# only those within SEMANTIC_BAND of the best one: a query that matches one
# paper well should not drag in fifty that match it vaguely. Calibrated on the
# real library with bge-small-en-v1.5; another model needs new values.
SEMANTIC_FLOOR = 0.60
SEMANTIC_BAND = 0.12
SEMANTIC_DEPTH = 60          # never more semantic matches than this
RRF_K = 60                   # reciprocal rank fusion: score = sum of 1 / (K + rank)


def surnames(conn: sqlite3.Connection) -> set[str]:
    """Every author surname in the library, lowercase (to tell names from words)."""
    names: set[str] = set()
    for row in conn.execute("SELECT g_authors FROM files WHERE g_authors IS NOT NULL"):
        names.update(n.lower() for n in json.loads(row["g_authors"]) if n)
    return names


def _match_expression(terms: list[str], any_word: bool = False) -> str:
    """The FTS5 query for the words: each quoted (so punctuation is harmless) and
    prefix-matched; a known abbreviation also matches its expansion ("IO" finds
    "industrial organization")."""
    parts = []
    for term in terms:
        word = '"' + term.replace('"', '""') + '"*'
        if full := query.abbreviation(term):
            word = f'({word} OR "{full}")'
        parts.append(word)
    return (" OR " if any_word else " AND ").join(parts)


def search(
    conn: sqlite3.Connection, terms: list[str], filters: dict[str, str] | None = None,
    limit: int | None = 50, *, semantic: bool = False, wide: bool = False,
    report: dict | None = None,
) -> list[dict]:
    """Find files by free text and/or `key:value` filters.

    Free-text words must all match (prefix match, so "demand est" works).
    A file passes a path filter if *any* of its locations satisfies it.

    semantic=True also ranks items by meaning (embed.py) and merges the two
    rankings, so "IO paper on ice cream" finds a paper whose summary speaks of
    frozen-dessert demand. It is used for searches people type (`find`, the
    web page); commands that act on what they select (`process`, `rename`)
    keep the exact keyword match.
    wide=True builds a shortlist for ask.py instead: any one word may match,
    and semantic matches are not cut off by similarity.
    report, if given, receives how sure the ranking is: keyword_hits,
    top_similarity and margin (best similarity minus the fifth best).
    """
    filters = {FILTER_ALIASES.get(k, k): v.lower() for k, v in (filters or {}).items()}
    unknown = set(filters) - PATH_FILTERS - FILE_FILTERS
    if unknown:
        raise ValueError(f"unknown filter(s): {', '.join(sorted(unknown))}")
    terms = [t for t in terms if t.strip()]
    if report is not None:
        report.update(keyword_hits=0, top_similarity=None, margin=None, semantic_used=False)

    # Candidate files, best text match first (or all files when there is no text).
    if terms:
        rows = conn.execute(
            "SELECT f.* FROM search JOIN files f ON f.hash = search.hash "
            # Column weights (hash, title, authors, aliases, filename, context,
            # venue, tags, description, notes): a hit on initials or citekey
            # counts most, a word buried in the description least.
            "WHERE search MATCH ? "
            "ORDER BY bm25(search, 0, 5.0, 5.0, 10.0, 2.0, 1.0, 1.0, 2.0, 0.5, 1.0)",
            # A wide search leaves out describing words ("paper", "on"), which
            # would otherwise match nearly everything once any word may match.
            (_match_expression([t for t in terms if t.lower() not in query.FILLER] or terms,
                               any_word=wide) if wide else _match_expression(terms),),
        ).fetchall()
        if report is not None:
            report["keyword_hits"] = len(rows)

        # Meaning, for queries of several words or ones the words alone miss;
        # never for a query of names and identifiers only ("blp", "chetty
        # looney", a citekey): that is a lookup, and similarity means nothing.
        # The model gets the words as typed. Spelling out "IO" here would make
        # the field outweigh the topic ("IO paper on ice cream" then finds IO
        # syllabi); the keyword side already matches the expansion.
        lookup_only = all(query.is_lookup(t, surnames(conn)) for t in terms)
        if semantic and not lookup_only and (wide or len(terms) >= 2 or not rows):
            ranked = embed.rank(conn, " ".join(terms))
            if ranked:
                top = ranked[0][1]
                if report is not None:
                    report.update(semantic_used=True, top_similarity=round(top, 4),
                                  margin=round(top - ranked[min(4, len(ranked) - 1)][1], 4))
                if not wide:
                    ranked = [(u, s) for u, s in ranked
                              if s >= max(SEMANTIC_FLOOR, top - SEMANTIC_BAND)]
                ranked = ranked[:SEMANTIC_DEPTH]
                # Reciprocal rank fusion: each list contributes 1 / (K + rank).
                # A keyword hit gets a hair more, so a tie goes to the exact match.
                score: dict[str, float] = {}
                for position, row in enumerate(rows):
                    score.setdefault(embed.unit_of(row), 1 / (RRF_K + position) + 1e-6)
                for position, (unit, _) in enumerate(ranked):
                    score[unit] = score.get(unit, 0.0) + 1 / (RRF_K + position)
                rows = [r for r in conn.execute("SELECT * FROM files") if embed.unit_of(r) in score]
                rows.sort(key=lambda r: -score[embed.unit_of(r)])
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
            if key == "status" and (row["status"] or "unprocessed") != wanted:
                return False
            if key == "tag" and wanted.replace("-", " ") not in (row["tags"] or "").lower():
                return False
            if key in PATH_FILTERS:
                def hit(loc: sqlite3.Row) -> bool:
                    value = (loc[key] or "").lower()
                    # context and topic match loosely; the rest must be exact.
                    return wanted in value if key in {"context", "topic"} else value == wanted
                if not any(hit(loc) for loc in locs):
                    return False
        return True

    # One result per work: several files of the same paper are merged, and
    # their locations listed together.
    results: list[dict] = []
    seen_works: dict[str, dict] = {}
    for row in rows:
        # A stand-in row for a work without a PDF has no locations; any other
        # row without a location is a file that has left the disk.
        if row["hash"] not in locations and not row["hash"].startswith("work:"):
            continue
        row_locations = locations.get(row["hash"], [])
        if not passes(row, row_locations):
            continue
        item = _file_dict(row, row_locations)
        earlier = seen_works.get(item["work_id"]) if item["work_id"] else None
        if earlier is not None:
            earlier["locations"] += item["locations"]
            earlier["hashes"].append(item["hash"])
            continue
        item["hashes"] = [item["hash"]]
        if item["work_id"]:
            seen_works[item["work_id"]] = item
        results.append(item)

    if terms:
        # Papers first: library works rank above slides and other course material
        # that merely mention the same words. (sort is stable: relevance order
        # is kept within each group.)
        results.sort(key=lambda r: r["work_id"] is None)
    if not terms:   # no relevance ranking available: order like the folder tree
        def folder_order(r: dict) -> tuple:
            if not r["locations"]:
                return ("~", 999, r["title"] or "")          # no file: after everything else
            first = r["locations"][0]
            return (first["context"] or "",
                    first["topic_order"] if first["topic_order"] is not None else 999,
                    first["path"])
        results.sort(key=folder_order)
    return results[:limit] if limit else results
