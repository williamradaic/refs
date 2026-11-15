"""Read records: the per-file results the pipeline stores in Dropbox.

Kept apart from pipeline.py so that quick commands such as `refs find` can read
the records without loading the PDF and API libraries.
"""

from __future__ import annotations

import json
from pathlib import Path

from .config import Config

LIBRARY_KINDS = {"paper", "book", "chapter"}


def reads_dir(cfg: Config) -> Path:
    return cfg.absolute(cfg.state_dir) / "reads"


def read_json(path: Path) -> dict | None:
    """Load a JSON file; None if it is missing or still being written."""
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None


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
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps({**data, "schema": SCHEMA}, indent=1, ensure_ascii=False))
    temporary.replace(path)


def load_read(cfg: Config, file_hash: str) -> dict | None:
    return read_json(reads_dir(cfg) / f"{file_hash}.json")


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
    if reads_dir(cfg).is_dir():
        for path in reads_dir(cfg).glob("*.json"):
            for topic in ((read_json(path) or {}).get("reading") or {}).get("topics") or []:
                counts[topic.lower()] = counts.get(topic.lower(), 0) + 1
    return sorted(counts, key=lambda t: -counts[t])
