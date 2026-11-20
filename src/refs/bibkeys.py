"""Learn the citation keys your project .bib files use for library papers.

A project may cite Berry, Levinsohn and Pakes as `BLP95` while refs calls the
paper `berry1995automobile`. Each time the library is synced, refs reads the
.bib files listed in the `bib_files` setting, matches every entry to a work
(same DOI, or the same title ignoring case, braces and punctuation) and keeps
the project's key on the work under `bib_keys`. The search index includes
those keys, so `refs find BLP95` finds the paper, and an agent reading a .tex
file can resolve `\\cite{BLP95}`.

The .bib files are only read, never changed. The keys are recomputed on every
sync, so a key that disappears from its file disappears from refs too.
"""

from __future__ import annotations

import glob
import os
from pathlib import Path

from . import bib
from .config import Config


def bib_paths(cfg: Config) -> list[Path]:
    """The .bib files the `bib_files` patterns, and the collections, point to.

    A pattern starting with "~" or "/" is a path on this Mac; anything else is
    relative to the Dropbox root. `**` matches any depth of folders.
    """
    from . import collection

    found: set[Path] = set()
    # Also the .bib file of each registered collection (`refs project new --bib`).
    own = [e["bib_file"] for e in collection.load(cfg).values() if e.get("bib_file")]
    for pattern in [*cfg.bib_files, *own]:
        base = os.path.expanduser(pattern) if pattern.startswith(("~", "/")) \
            else str(cfg.dropbox_root / pattern)
        for match in glob.glob(base, recursive=True):
            path = Path(match)
            if path.suffix == ".bib" and path.is_file():
                found.add(path.resolve())
    return sorted(found)


def label(cfg: Config, path: Path) -> str:
    """A short, machine-independent name for a .bib file, for display."""
    try:
        return "Dropbox/" + str(path.relative_to(cfg.dropbox_root))
    except ValueError:
        pass
    try:
        return "~/" + str(path.relative_to(Path.home()))
    except ValueError:
        return str(path)


def learn(cfg: Config, works: dict[str, dict]) -> dict:
    """Set `bib_keys` ({key: .bib file}) on every work some .bib file cites.

    Changes `works` in place; the caller writes the works that changed.
    Returns a summary: files read, entries, entries matched.
    """
    by_doi: dict[str, set[str]] = {}
    by_title: dict[str, set[str]] = {}
    for wid, work in works.items():
        meta = work.get("meta") or {}
        if meta.get("doi"):
            by_doi.setdefault(meta["doi"].lower(), set()).add(wid)
        if title := bib._norm_title(meta.get("title") or ""):
            by_title.setdefault(title, set()).add(wid)

    learned: dict[str, dict[str, str]] = {}
    summary = {"files": 0, "entries": 0, "matched": 0}
    for path in bib_paths(cfg):
        try:
            entries = bib.read_entries(path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
        summary["files"] += 1
        summary["entries"] += len(entries)
        for entry in entries:
            # A DOI is decisive; a title only counts if exactly one work has it.
            candidates = by_doi.get(entry["doi"] or "") or by_title.get(entry["title"] or "") or set()
            if len(candidates) != 1:
                continue
            [wid] = candidates
            summary["matched"] += 1
            if entry["key"] != works[wid].get("citekey"):
                learned.setdefault(wid, {}).setdefault(entry["key"], label(cfg, path))

    for wid, work in works.items():
        keys = learned.get(wid, {})
        if keys:
            work["bib_keys"] = dict(sorted(keys.items()))
        else:
            work.pop("bib_keys", None)
    return summary
