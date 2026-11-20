"""Tidying the library from the web page: filing PDFs and identical copies.

* File to…: move a PDF into another folder under the scanned roots (typically
  out of papers-dump into a course or project folder). This is the only way
  refs moves a file. The file keeps its name, and the move is journaled like
  a rename, so `refs rename --undo` reverses it.
* Copy to…: the same, but the original stays put; for a PDF that has to sit
  in a shared project folder. Undo sends the copy to the Trash.
* Copies: the same PDF stored in more than one place. Each extra copy can be
  moved to the Trash (never the last one), or the set can be marked as kept on
  purpose, which takes it out of the Review queue (and can be un-kept).

* New folder: create a folder (say, a project's literature folder) to file into.
* Many at once: file or copy a whole selection in one step (one scan after).

Files that other tools manage (canvas-sync, overleaf, Git repositories) are
never moved or trashed, and nothing is ever moved into them.
"""

from __future__ import annotations

import os
import shutil
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

def copies(cfg: Config, conn: sqlite3.Connection, *, kept: bool = False) -> list[dict]:
    """Files stored in more than one place: the sets still to review, or with
    `kept=True` the sets you marked as on purpose (so they can be un-kept).

    A library copy plus copies in the shared folders of collections the paper
    is in (shared.py) is how it should be, so such a set is never listed.
    """
    from . import shared                                # shared.py uses this module
    kept_hashes = set(_review(cfg)["kept_copies"])
    shared_folders = shared.folders(cfg)
    found = []
    for item in index.search(conn, [], {}, limit=None):
        paths = [loc["path"] for loc in item["locations"]]
        if len(paths) < 2 or (item["hash"] in kept_hashes) != kept:
            continue
        if shared.recorded(cfg, paths, item.get("collections") or [], shared_folders):
            continue
        found.append(item)
    return found


def keep_copies(cfg: Config, file_hash: str) -> None:
    review = _review(cfg)
    if file_hash not in review["kept_copies"]:
        review["kept_copies"].append(file_hash)
        records.write_json(_review_path(cfg), review)


def unkeep_copies(cfg: Config, file_hash: str) -> None:
    """Undo `keep_copies`: the set comes back to the Review queue."""
    review = _review(cfg)
    if file_hash in review["kept_copies"]:
        review["kept_copies"].remove(file_hash)
        records.write_json(_review_path(cfg), review)


def trash(cfg: Config, conn: sqlite3.Connection, rel: str, *, other_version_ok: bool = False) -> Path:
    """Move one copy of a file to the macOS Trash. Returns where it went.

    Refused for the last copy of a file and inside managed folders. With
    `other_version_ok` (a recommendation of keep.py), the last copy of one
    version may go while the paper keeps a file of another version (a working
    paper next to the published article); never the paper's last file. The
    file keeps its name in the Trash, so it can be dragged back.
    """
    file_hash = _hash_of(conn, rel)
    others = conn.execute("SELECT COUNT(*) FROM paths WHERE hash=? AND path<>?",
                          (file_hash, rel)).fetchone()[0]
    if others == 0 and other_version_ok:
        others = conn.execute(
            "SELECT COUNT(*) FROM paths p JOIN files f ON f.hash = p.hash WHERE p.path<>? AND f.work_id = "
            "(SELECT work_id FROM files WHERE hash=? AND work_id IS NOT NULL)", (rel, file_hash)).fetchone()[0]
    if others == 0:
        raise Refused("this is the only copy; refs never trashes the last one")
    if reason := rename.blocked_reason(cfg, rel):
        raise Refused(f"refs leaves this file alone: {reason}")
    return rename.to_trash(cfg.absolute(rel))


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
    roots = set(cfg.watched)
    for (path,) in conn.execute("SELECT path FROM paths"):
        folder = PurePosixPath(path).parent
        while str(folder) not in roots and str(folder) != ".":
            folders.add(str(folder))
            folder = folder.parent
    allowed = [f for f in folders
               if not (f == cfg.dump or f.startswith(cfg.dump + "/")) and _folder_allowed(cfg, f) is None]
    return sorted(allowed)


def _target(cfg: Config, conn: sqlite3.Connection, rel: str, folder: str) -> tuple[str, Path, Path]:
    """Check that `rel` may be filed into `folder`. Returns (hash, source, target)."""
    file_hash = _hash_of(conn, rel)
    if not cfg.is_watched(folder):
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
    return file_hash, source, target


def move(cfg: Config, conn: sqlite3.Connection, rel: str, folder: str) -> str:
    """Move one PDF into `folder` (Dropbox-relative). Returns its new path."""
    folder = folder.strip("/")
    if reason := rename.blocked_reason(cfg, rel):
        raise Refused(f"refs leaves this file alone: {reason}")
    file_hash, source, target = _target(cfg, conn, rel, folder)
    os.rename(source, target)
    new_rel = f"{folder}/{source.name}"
    rename.journal(cfg, rel, new_rel, file_hash, action="move")
    return new_rel


def copy(cfg: Config, conn: sqlite3.Connection, rel: str, folder: str) -> str:
    """Copy one PDF into `folder`, leaving the original where it is. Returns the copy's path.

    For a PDF that must physically sit in a project folder (one shared with
    coauthors, say). Unlike a move, the source may be in a managed folder: it
    is only read. The pair is marked as kept on purpose, so the copy does not
    turn up in the Review queue.
    """
    folder = folder.strip("/")
    file_hash, source, target = _target(cfg, conn, rel, folder)
    shutil.copy2(source, target)                   # keeps the modification time
    new_rel = f"{folder}/{source.name}"
    rename.journal(cfg, rel, new_rel, file_hash, action="copy")
    keep_copies(cfg, file_hash)
    return new_rel


# --- New folders ------------------------------------------------------------------------------

def make_folder(cfg: Config, folder: str, *, anywhere: bool = False) -> str:
    """Create `folder` (library-relative), with any missing folders above it,
    so PDFs can be filed into it. Returns the cleaned path.

    Same limits as filing: inside the scanned roots, never in a managed folder
    (overleaf, Git repositories, ...) or refs' own. Names that start with "."
    or contain ".." are refused, so nothing can point outside the library.
    `anywhere` lifts the first limit for a shared collection's folder, which
    is watched because it is shared.
    """
    parts = [p.strip() for p in folder.strip().strip("/").split("/")]
    if not parts or any(not p or p.startswith(".") for p in parts):
        raise Refused("folder names cannot be empty or start with a dot")
    if any(ch in p for p in parts for ch in ("\\", ":", "\0")):
        raise Refused("folder names cannot contain \\, : or control characters")
    clean = "/".join(parts)
    if not anywhere and not any(clean.startswith(root + "/") for root in cfg.watched):
        raise Refused("new folders go inside one of the folders refs scans")
    if reason := _folder_allowed(cfg, clean):
        raise Refused(f"cannot create a folder there: {reason}")
    target = cfg.absolute(clean)
    if target.exists() and not target.is_dir():
        raise Refused(f"{clean} exists and is not a folder")
    target.mkdir(parents=True, exist_ok=True)
    return clean


# --- Many at once -----------------------------------------------------------------------------

def file_many(cfg: Config, conn: sqlite3.Connection, paths: list[str], folder: str,
              mode: str = "move") -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """Move (or copy) several PDFs into `folder`. Each file is tried on its
    own, so one refusal (a name already there, say) does not stop the rest.
    Returns ([(path, new path)], [(path, why it was refused)]). Every success
    is one journal entry, so undoing len(done) steps reverses the batch."""
    act = copy if mode == "copy" else move
    done, refused = [], []
    for rel in dict.fromkeys(paths):                 # each path once, in order
        try:
            done.append((rel, act(cfg, conn, rel, folder)))
        except Refused as error:
            refused.append((rel, str(error)))
        except OSError as error:                     # e.g. the file vanished meanwhile
            refused.append((rel, f"{type(error).__name__}: {error}"))
    return done, refused
