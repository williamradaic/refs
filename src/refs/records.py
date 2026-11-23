"""Read records: the per-file results the pipeline stores in Dropbox.

Kept apart from pipeline.py so that quick commands such as `refs find` can read
the records without loading the PDF and API libraries.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

from .config import Config

LIBRARY_KINDS = {"paper", "book", "chapter"}

# Stat-validated cache of raw JSON text for read/work files: path -> ((ino, size, mtime_ns), text).
# Keying on inode + size + mtime_ns guarantees instant invalidation whenever a file is rewritten
# (atomic replace creates a new inode) or edited externally, while avoiding repeated disk reads.
_JSON_CACHE_MAX = 8192
_json_cache: dict[str, tuple[tuple[int, int, int], str]] = {}
_json_cache_lock = threading.Lock()


def reads_dir(cfg: Config) -> Path:
    return cfg.absolute(cfg.state_dir) / "reads"


def read_json(path: Path, *, _stat: os.stat_result | None = None) -> dict | None:
    """Load a JSON file; None if it is missing or still being written."""
    key = str(path)
    try:
        st = _stat if _stat is not None else path.stat()
        sig = (st.st_ino, st.st_size, st.st_mtime_ns)
    except FileNotFoundError:
        with _json_cache_lock:
            _json_cache.pop(key, None)
        return None
    except OSError:
        return None
    with _json_cache_lock:
        cached = _json_cache.get(key)
    if cached is not None and cached[0] == sig:
        try:
            return json.loads(cached[1])
        except json.JSONDecodeError:
            return None
    try:
        text = path.read_text(encoding="utf-8")
        data = json.loads(text)
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    with _json_cache_lock:
        if len(_json_cache) >= _JSON_CACHE_MAX:
            _json_cache.clear()
        _json_cache[key] = (sig, text)
    return data


# The version of the JSON files refs keeps in Dropbox (works, read records,
# review.json); each file carries it as "schema". Raise it when their shape
# changes in a way an older refs would get wrong, and have read_json upgrade
# older files. A file written by a newer refs is read but never overwritten, so
# an older copy of refs on another Mac cannot undo what the newer one wrote.
SCHEMA = 1


class NewerData(RuntimeError):
    """A file was written by a newer version of refs: update this one."""


def write_json(path: Path, data: dict) -> None:
    """Write a JSON file in one step, so a reader never sees half a file."""
    existing = read_json(path)
    if existing is not None and int(existing.get("schema", 0)) > SCHEMA:
        raise NewerData(f"{path.name} was written by a newer version of refs (data version "
                        f"{existing['schema']}, this one knows {SCHEMA}). Update refs with "
                        "`uv tool upgrade refs`; nothing was overwritten.")
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps({**data, "schema": SCHEMA}, indent=1, ensure_ascii=False)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        temporary.write_text(text, encoding="utf-8")
        temporary.replace(path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    try:
        st = path.stat()
        with _json_cache_lock:
            _json_cache[str(path)] = ((st.st_ino, st.st_size, st.st_mtime_ns), text)
    except OSError:
        with _json_cache_lock:
            _json_cache.pop(str(path), None)


def load_read(cfg: Config, file_hash: str) -> dict | None:
    return read_json(reads_dir(cfg) / f"{file_hash}.json")


def load_all_reads(cfg: Config) -> dict[str, dict]:
    """Every read record, keyed by file hash (single scandir, stat-cached)."""
    folder = reads_dir(cfg)
    if not folder.is_dir():
        return {}
    out: dict[str, dict] = {}
    try:
        with os.scandir(folder) as entries:
            for entry in entries:
                if not entry.name.endswith(".json") or entry.name.startswith("."):
                    continue
                try:
                    st = entry.stat()
                except OSError:
                    continue
                read = read_json(Path(entry.path), _stat=st)
                if read:
                    out[entry.name[:-5]] = read
    except FileNotFoundError:
        pass
    return out


def final_metadata(read: dict) -> dict:
    """The metadata the library shows for a read record, and where it came from.

    Registry facts when there is a registry record; otherwise what the model
    transcribed from the page.
    """
    registry, reading = read.get("registry"), read.get("reading") or {}
    if registry:
        record = registry["record"]
        return {
            "title": record["title"], "venue": record["venue"],
            "year": registry.get("year_printed") or record["year"],
            "surnames": [a["family"] for a in record["authors"] if a.get("family")],
            "doi": record["doi"], "source": record["source"],
        }
    return {
        "title": reading.get("title"), "year": reading.get("year"),
        "venue": reading.get("venue"),
        "surnames": [a["family"] for a in reading.get("authors") or [] if a.get("family")],
        "doi": None, "source": "pdf",
    }


def known_topics(cfg: Config) -> list[str]:
    """Topics already used in the library, most frequent first."""
    counts: dict[str, int] = {}
    for read in load_all_reads(cfg).values():
        for topic in (read.get("reading") or {}).get("topics") or []:
            key = topic.lower()
            counts[key] = counts.get(key, 0) + 1
    return sorted(counts, key=lambda t: -counts[t])
