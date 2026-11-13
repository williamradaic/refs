"""Semantic search: each item's text as a vector, compared with the query's.

A small model that runs on this Mac (fastembed + BAAI/bge-small-en-v1.5 by
default; `embedding_model` in config.toml) turns text into 384 numbers such
that texts with similar meaning get similar numbers. "IO paper on ice cream"
then lands near a summary about "demand for frozen desserts", with no word
in common. Nothing leaves the machine, and nothing is paid for.

What is embedded, per item (a work, or a file that belongs to no work):
  part 0     title, authors, venue, year, tags, the summary from the reading step
  part 1..n  your reading note, in pieces of about a paragraph or two

Vectors live in the `vectors` table of the index, with a fingerprint of the
text they came from. refresh() runs after every scan and embeds only what is
new or changed, so after the first run (about a minute) it costs next to
nothing. The model is loaded only when something needs embedding, or when a
query does.

Scores are cosine similarities: 1 = same meaning, around 0.5 = unrelated for
this model. An item's score is that of its best-matching part.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Callable

import numpy as np

from . import notes
from .config import Config

SCHEMA = """
CREATE TABLE IF NOT EXISTS vectors (
    unit    TEXT NOT NULL,          -- work id, or file hash for a file without a work
    part    INTEGER NOT NULL,       -- 0 = the record, 1.. = pieces of the note
    digest  TEXT NOT NULL,          -- fingerprint of model + text
    vector  BLOB NOT NULL,          -- float32, length 1
    PRIMARY KEY (unit, part)
);
"""

# Models in the bge family are trained to find passages for a query written
# with this instruction in front; documents are embedded without it.
QUERY_INSTRUCTION = {
    "BAAI/bge-small-en-v1.5": "Represent this sentence for searching relevant passages: ",
    "BAAI/bge-base-en-v1.5": "Represent this sentence for searching relevant passages: ",
    "BAAI/bge-large-en-v1.5": "Represent this sentence for searching relevant passages: ",
}

# Where the model files are kept. fastembed's default is a temporary folder
# that macOS empties now and then, which would mean downloading it again.
CACHE_DIR = Path.home() / "Library" / "Caches" / "refs" / "fastembed"

NOTE_PIECE_CHARS = 1200     # about 300 tokens; the model reads at most 512


# --- The model ------------------------------------------------------------------

# An encoder takes texts and whether they are queries, and returns one
# unit-length row per text. Tests swap in a tiny fake with set_encoder().
Encoder = Callable[[list[str], bool], np.ndarray]
_encoders: dict[str, Encoder] = {}
_override: Encoder | None = None


def set_encoder(encoder: Encoder | None) -> None:
    """Use this function instead of the real model (tests), or None to undo."""
    global _override
    _override = encoder
    _matrices.clear()


def _fastembed(model_name: str) -> Encoder:
    from fastembed import TextEmbedding          # imported here: it takes ~0.3 s

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    model = TextEmbedding(model_name, cache_dir=str(CACHE_DIR))
    instruction = QUERY_INSTRUCTION.get(model_name, "")

    def encode(texts: list[str], query: bool) -> np.ndarray:
        if query:
            texts = [instruction + t for t in texts]
        rows = np.array(list(model.embed(texts, batch_size=64)), dtype=np.float32)
        return rows / np.maximum(np.linalg.norm(rows, axis=1, keepdims=True), 1e-12)

    return encode


def encoder(model_name: str) -> Encoder:
    if _override is not None:
        return _override
    if model_name not in _encoders:          # loaded once per process
        _encoders[model_name] = _fastembed(model_name)
    return _encoders[model_name]


# --- What each item says -------------------------------------------------------

def _pieces(text: str, size: int = NOTE_PIECE_CHARS) -> list[str]:
    """Split a note at paragraph breaks into pieces of at most about `size` characters."""
    pieces, current = [], ""
    for paragraph in (p.strip() for p in text.split("\n\n")):
        if not paragraph:
            continue
        while len(paragraph) > size:              # one very long paragraph: cut it
            pieces.append(paragraph[:size])
            paragraph = paragraph[size:]
        if current and len(current) + len(paragraph) > size:
            pieces.append(current)
            current = ""
        current = f"{current}\n\n{paragraph}" if current else paragraph
    if current:
        pieces.append(current)
    return pieces


def unit_of(row: sqlite3.Row | dict) -> str:
    """The item a file row belongs to: its work if it has one, else the file itself."""
    return row["work_id"] or row["hash"]


def unit_texts(cfg: Config, conn: sqlite3.Connection) -> dict[str, list[str]]:
    """The texts to embed for every item in the index, part 0 first."""
    topics: dict[str, set[str]] = {}
    for loc in conn.execute("SELECT hash, topic, section FROM paths"):
        # Every file on disk gets an entry, even without a topic: it marks the file as present.
        topics.setdefault(loc["hash"], set()).update(filter(None, [loc["topic"], loc["section"]]))
    note_text = notes.bodies(cfg)

    texts: dict[str, list[str]] = {}
    for row in conn.execute("SELECT * FROM files ORDER BY hash"):
        unit = unit_of(row)
        if unit in texts:                 # another file of the same work: already done
            continue
        if row["hash"] not in topics and not row["hash"].startswith("work:"):
            continue                      # a file that has left the disk
        authors = json.loads(row["g_authors"] or "[]")
        tags = json.loads(row["tags"] or "[]")
        record = ". ".join(filter(None, [
            row["g_title"],
            ", ".join(authors),
            " ".join(filter(None, [row["venue"], str(row["g_year"] or "")])),
            ", ".join(tags),
            ", ".join(sorted(topics.get(row["hash"], ()))),
            row["description"],
        ]))
        parts = [record]
        if row["citekey"] and (body := note_text.get(row["citekey"])):
            title = row["g_title"] or row["citekey"]
            parts += [f"{title}. My notes: {piece}" for piece in _pieces(body)]
        texts[unit] = parts
    return texts


# --- Keeping the vectors current --------------------------------------------------

def _digest(model_name: str, text: str) -> str:
    return hashlib.sha256(f"{model_name}\n{text}".encode()).hexdigest()[:20]


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def refresh(cfg: Config, conn: sqlite3.Connection) -> int:
    """Embed what is new or changed, drop what is gone. Returns how many texts it embedded."""
    ensure_schema(conn)
    model_name = cfg.embedding_model
    row = conn.execute("SELECT value FROM meta WHERE key='embed_model'").fetchone()
    if row is None or row["value"] != model_name:
        conn.execute("DELETE FROM vectors")       # another model: its vectors do not compare
        conn.execute("INSERT OR REPLACE INTO meta VALUES ('embed_model', ?)", (model_name,))

    wanted = {(unit, part): text for unit, parts in unit_texts(cfg, conn).items()
              for part, text in enumerate(parts)}
    have = {(r["unit"], r["part"]): r["digest"]
            for r in conn.execute("SELECT unit, part, digest FROM vectors")}

    stale = [key for key in have if key not in wanted]
    todo = [(key, text) for key, text in wanted.items()
            if have.get(key) != _digest(model_name, text)]
    for unit, part in stale:
        conn.execute("DELETE FROM vectors WHERE unit=? AND part=?", (unit, part))

    if todo:
        encode = encoder(model_name)
        for start in range(0, len(todo), 256):
            batch = todo[start:start + 256]
            rows = encode([text for _, text in batch], False)
            conn.executemany(
                "INSERT OR REPLACE INTO vectors (unit, part, digest, vector) VALUES (?,?,?,?)",
                [(unit, part, _digest(model_name, text), rows[i].astype(np.float32).tobytes())
                 for i, ((unit, part), text) in enumerate(batch)],
            )
            if sys.stderr.isatty() and len(todo) > 256:
                print(f"\r  embedding {min(start + 256, len(todo)):,}/{len(todo):,}",
                      end="" if start + 256 < len(todo) else "\n", file=sys.stderr)
    if todo or stale:
        # Tells running processes that their copy of the vectors is out of date.
        conn.execute("INSERT OR REPLACE INTO meta VALUES ('vectors_version', ?)",
                     (os.urandom(6).hex(),))
    return len(todo)


# --- Ranking ------------------------------------------------------------------------

# The vectors of one index, kept in memory between queries (the web server
# answers many). Reloaded when vectors_version changes.
_matrices: dict[str, tuple[str, list[str], np.ndarray]] = {}


def _matrix(conn: sqlite3.Connection) -> tuple[list[str], np.ndarray]:
    """(unit of each row, matrix of vectors) for the whole index."""
    db = conn.execute("PRAGMA database_list").fetchone()["file"]
    version = conn.execute("SELECT value FROM meta WHERE key='vectors_version'").fetchone()
    version = version["value"] if version else ""
    cached = _matrices.get(db)
    if cached and cached[0] == version:
        return cached[1], cached[2]
    units, rows = [], []
    for row in conn.execute("SELECT unit, vector FROM vectors ORDER BY unit, part"):
        units.append(row["unit"])
        rows.append(np.frombuffer(row["vector"], dtype=np.float32))
    matrix = np.vstack(rows) if rows else np.zeros((0, 1), dtype=np.float32)
    _matrices[db] = (version, units, matrix)
    return units, matrix


def rank(conn: sqlite3.Connection, text: str) -> list[tuple[str, float]]:
    """Every item, most similar to `text` first, as (unit, cosine similarity).

    Empty if the library has not been embedded yet.
    """
    ensure_schema(conn)
    units, matrix = _matrix(conn)
    if not units:
        return []
    model = conn.execute("SELECT value FROM meta WHERE key='embed_model'").fetchone()
    query = encoder(model["value"])([text], True)[0]
    scores = matrix @ query
    best: dict[str, float] = {}
    for unit, score in zip(units, scores.tolist()):
        if score > best.get(unit, -2.0):
            best[unit] = score
    return sorted(best.items(), key=lambda kv: -kv[1])
