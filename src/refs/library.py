"""The library folder: where every reading lives once, under its canonical name.

The library folder is the quick-add folder (`dump` in config.toml): papers-dump
before the migration, refs-library after it. A course or project is a
collection (collection.py), not a folder, so a paper needs only this one copy;
the exceptions are shared project folders (shared.py), which get recorded
copies for coauthors.

`place` brings one PDF in from elsewhere: moved (or copied, when the source
must stay where it is) and named `surname-year-short-title.pdf` when its
record is verified or confirmed. Every placement is journaled like a
rename, so `refs rename --undo` (and the migration's undo) reverses it.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
from pathlib import PurePosixPath

from . import rename
from .config import Config


def folder(cfg: Config) -> str:
    return cfg.dump.strip("/")


def holds(cfg: Config, rel: str) -> bool:
    """Is this library-relative path inside the library folder?"""
    return rel == folder(cfg) or rel.startswith(folder(cfg) + "/")


def copy_of(cfg: Config, paths: list[str]) -> str | None:
    """Of a work's paths, the library copy: the first inside the library folder."""
    return next((p for p in sorted(paths) if holds(cfg, p)), None)


def first_by_library(cfg: Config, locations: list[dict]) -> list[dict]:
    """Locations with the library copy first (the one to open, cite and point
    agents to), the others after it in their own order."""
    return sorted(locations, key=lambda loc: not holds(cfg, loc["path"]))


def paths_of(conn: sqlite3.Connection, work_id: str) -> list[str]:
    """Every path on disk of a work's files (all versions)."""
    return [r["path"] for r in conn.execute(
        "SELECT p.path FROM paths p JOIN files f ON f.hash = p.hash WHERE f.work_id = ? "
        "ORDER BY p.path", (work_id,))]


class Refused(Exception):
    """A placement refs will not make; the message says why."""


def name_for(cfg: Config, rel: str, work: dict | None, taken: set[str]) -> str:
    """The name a PDF gets in the library folder: canonical when its record
    can be trusted, else its own; never one in `taken`."""
    current = PurePosixPath(rel).name
    wanted = (rename.proposed_name(work, current) if work else None) or current
    is_wp = bool(((work or {}).get("meta") or {}).get("is_working_paper"))
    return rename.disambiguate(wanted, taken, is_wp)


def place(cfg: Config, conn: sqlite3.Connection, rel: str, work: dict | None, *,
          mode: str = "move", taken: set[str] | None = None) -> str:
    """Move (or copy) one PDF into the library folder. Returns its new path.

    `mode` "move" is refused for a file another tool manages (canvas-sync,
    Overleaf, a Git repository): pass "copy" for those. `taken` holds names
    already claimed in the library folder by earlier placements of a batch
    that have not happened yet; the names on disk are always avoided.
    """
    if mode not in {"move", "copy"}:
        raise Refused("mode is move or copy")
    if holds(cfg, rel):
        raise Refused("already in the library folder")
    row = conn.execute("SELECT hash FROM paths WHERE path=?", (rel,)).fetchone()
    file_hash = row["hash"] if row else None
    if mode == "move" and (reason := rename.blocked_reason(cfg, rel)):
        raise Refused(f"refs does not move this file ({reason}); copy it instead")
    source = cfg.absolute(rel)
    if not source.is_file():
        raise Refused("the file is no longer there")
    target_dir = cfg.absolute(folder(cfg))
    target_dir.mkdir(parents=True, exist_ok=True)
    on_disk = {p.name for p in target_dir.glob("*.pdf")}
    name = name_for(cfg, rel, work, on_disk | (taken or set()))
    target = target_dir / name
    if target.exists():                                  # never overwrite
        raise Refused(f"the library already has a file called {name}")
    if mode == "copy":
        shutil.copy2(source, target)                     # keeps the modification time
    else:
        os.rename(source, target)
    new_rel = f"{folder(cfg)}/{name}"
    rename.journal(cfg, rel, new_rel, file_hash, action=mode)
    if mode == "copy" and file_hash:
        # The original stays on purpose (a managed or shared folder): not a
        # copy to review.
        from . import organize
        organize.keep_copies(cfg, file_hash)
    return new_rel
