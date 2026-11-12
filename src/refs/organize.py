"""Tidying the library from the web page: filing PDFs and identical copies.

* File to…: move a PDF into another folder under the scanned roots (typically
  out of papers-dump into a course or project folder). This is the only way
  refs moves a file. The file keeps its name, and the move is journaled like
  a rename, so `refs rename --undo` reverses it.
* Copies: the same PDF stored in more than one place. Each extra copy can be
  moved to the Trash (never the last one), or the set can be marked as kept on
  purpose, which takes it out of the Review queue.

Files that other tools manage (canvas-sync, overleaf, Git repositories) are
never moved or trashed, and nothing is ever moved into them.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path, PurePosixPath

from . import index, records, rename
from .config import Config


class Refused(Exception):
    """An action refs will not take; the message says why."""


def _review_path(cfg: Config) -> Path:
    # Synced with the other records, so a "kept" decision holds on every Mac.
    return cfg.absolute(cfg.state_dir) / "review.json"


def _review(cfg: Config) -> dict:
    return records.read_json(_review_path(cfg)) or {"kept_copies": []}


def _hash_of(conn: sqlite3.Connection, rel: str) -> str:
    row = conn.execute("SELECT hash FROM paths WHERE path=?", (rel,)).fetchone()
    if row is None:
        raise Refused("refs does not know that file (rescan with R?)")
    return row["hash"]


# --- Identical copies -----------------------------------------------------------------

def copies(cfg: Config, conn: sqlite3.Connection) -> list[dict]:
    """Files stored in more than one place, minus the sets you chose to keep."""
    kept = set(_review(cfg)["kept_copies"])
    return [item for item in index.search(conn, [], {}, limit=None)
            if len(item["locations"]) > 1 and item["hash"] not in kept]


def keep_copies(cfg: Config, file_hash: str) -> None:
    review = _review(cfg)
    if file_hash not in review["kept_copies"]:
        review["kept_copies"].append(file_hash)
        records.write_json(_review_path(cfg), review)


def trash(cfg: Config, conn: sqlite3.Connection, rel: str) -> Path:
    """Move one copy of a file to the macOS Trash. Returns where it went.

    Refused for the last copy of a file and inside managed folders. The file
    keeps its name in the Trash, so it can be dragged back.
    """
    file_hash = _hash_of(conn, rel)
    others = conn.execute("SELECT COUNT(*) FROM paths WHERE hash=? AND path<>?",
                          (file_hash, rel)).fetchone()[0]
    if others == 0:
        raise Refused("this is the only copy; refs never trashes the last one")
    if reason := rename.blocked_reason(cfg, rel):
        raise Refused(f"refs leaves this file alone: {reason}")
    source = cfg.absolute(rel)
    trash_dir = Path.home() / ".Trash"
    target = trash_dir / source.name
    number = 2
    while target.exists():                         # never overwrite something in the Trash
        target = trash_dir / f"{source.stem} {number}{source.suffix}"
        number += 1
    os.rename(source, target)                      # same disk: a move, not a copy
    return target


# --- File to… -----------------------------------------------------------------------------

def _folder_allowed(cfg: Config, folder: str) -> str | None:
    """Why `folder` cannot receive a filed PDF, or None if it can."""
    if folder == cfg.state_dir or folder.startswith(cfg.state_dir + "/"):
        return "that is refs' own folder"
    return rename.blocked_reason(cfg, f"{folder}/x.pdf")


def destinations(cfg: Config, conn: sqlite3.Connection) -> list[str]:
    """Folders a PDF can be filed into: every folder that already holds an
    indexed PDF, and the folders above them, minus managed ones and the dump."""
    folders: set[str] = set()
    roots = set(cfg.roots)
    for (path,) in conn.execute("SELECT path FROM paths"):
        folder = PurePosixPath(path).parent
        while str(folder) not in roots and str(folder) != ".":
            folders.add(str(folder))
            folder = folder.parent
    allowed = [f for f in folders
               if not (f == cfg.dump or f.startswith(cfg.dump + "/")) and _folder_allowed(cfg, f) is None]
    return sorted(allowed)


def move(cfg: Config, conn: sqlite3.Connection, rel: str, folder: str) -> str:
    """Move one PDF into `folder` (Dropbox-relative). Returns its new path."""
    file_hash = _hash_of(conn, rel)
    folder = folder.strip("/")
    if reason := rename.blocked_reason(cfg, rel):
        raise Refused(f"refs leaves this file alone: {reason}")
    if not any(folder == root or folder.startswith(root + "/") for root in cfg.roots):
        raise Refused("that folder is outside the folders refs scans")
    if reason := _folder_allowed(cfg, folder):
        raise Refused(f"cannot file into that folder: {reason}")
    target_dir = cfg.absolute(folder)
    if not target_dir.is_dir():
        raise Refused("that folder does not exist")
    source = cfg.absolute(rel)
    target = target_dir / source.name
    if target.exists():
        raise Refused(f"{folder} already has a file called {source.name}")
    os.rename(source, target)
    new_rel = f"{folder}/{source.name}"
    rename.journal(cfg, rel, new_rel, file_hash, action="move")
    return new_rel
