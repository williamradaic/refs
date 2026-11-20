"""Collections shared with coauthors: copies in their folders.

A project shared with coauthors has a Dropbox shared folder (collections.json:
`shared` and `folder`). Coauthors see only that folder, so the library keeps
the paper and the shared folder gets a copy:

* Putting a paper in a shared collection copies its library file into the
  collection's folder (organize.copy: journaled, undoable).
* Taking it out never deletes anything by itself; the page asks whether the
  shared copy should go to the Trash.
* A PDF a coauthor adds to the shared folder is offered in the Inbox view
  (inbox.py), to copy into the library with the collection filled in.

A copy is *recorded* when the work has one location in the library and
another in the folder of a shared collection it belongs to. That is derived,
not stored, and recorded copies stay out of the Copies review.
"""

from __future__ import annotations

import sqlite3

from . import collection, library, organize
from .config import Config


def folders(cfg: Config) -> dict[str, str]:
    """Shared collections and their folders: {name: folder}."""
    return {name: entry["folder"].strip("/") for name, entry in collection.load(cfg).items()
            if entry.get("shared") and entry.get("folder")}


def _inside(rel: str, folder: str) -> bool:
    return rel.startswith(folder + "/")


def collection_at(cfg: Config, rel: str, shared: dict[str, str] | None = None) -> str | None:
    """The shared collection whose folder holds this path, if any."""
    shared = folders(cfg) if shared is None else shared
    return next((name for name, f in shared.items() if _inside(rel, f)), None)


def recorded(cfg: Config, paths: list[str], member_of: list[str],
             shared: dict[str, str] | None = None) -> bool:
    """Is this set of locations a library copy plus copies in the shared
    folders of collections the work is in (and nothing else)?"""
    shared = folders(cfg) if shared is None else shared
    if not any(library.holds(cfg, p) for p in paths):
        return False
    elsewhere = [p for p in paths if not library.holds(cfg, p)]
    mine = [shared[n] for n in member_of if n in shared]
    return bool(elsewhere) and all(any(_inside(p, f) for f in mine) for p in elsewhere)


def copy_in(cfg: Config, conn: sqlite3.Connection, work_ids: list[str], name: str) -> dict:
    """Copy each work's library file into the shared collection's folder,
    unless it has a file there already. Returns {"copied": [(work id, path)],
    "refused": [(work id, why)]}. The caller scans afterwards."""
    folder = folders(cfg).get(name)
    copied, refused = [], []
    if folder is None:
        return {"copied": copied, "refused": refused}
    try:
        organize.make_folder(cfg, folder, anywhere=True)  # a new shared folder may not exist yet
    except organize.Refused as error:
        return {"copied": copied, "refused": [(w, str(error)) for w in work_ids]}
    for work_id in dict.fromkeys(work_ids):
        paths = library.paths_of(conn, work_id)
        if not paths:
            refused.append((work_id, "no PDF yet"))
            continue
        if any(_inside(p, folder) for p in paths):
            continue                                      # coauthors have it already
        source = library.copy_of(cfg, paths) or paths[0]
        try:
            copied.append((work_id, organize.copy(cfg, conn, source, folder)))
        except (organize.Refused, OSError) as error:
            refused.append((work_id, str(error)))
    return {"copied": copied, "refused": refused}


def copies_of(cfg: Config, conn: sqlite3.Connection, work_ids: list[str], name: str) -> list[str]:
    """The works' files inside a shared collection's folder (what taking them
    out of the collection could send to the Trash, if you say so)."""
    folder = folders(cfg).get(name)
    if folder is None:
        return []
    return [p for w in dict.fromkeys(work_ids) for p in library.paths_of(conn, w) if _inside(p, folder)]


def collect(cfg: Config, conn: sqlite3.Connection, work_ids: list[str], names: list[str]) -> list[str]:
    """Put works in collections, with what that brings: a copy in a shared
    collection's folder, and the index updated at once. Returns the clean
    names used (a name that cannot be one is skipped)."""
    from . import index, ingest, works
    from . import scan as scanner

    used = []
    for raw in names:
        try:
            name = collection.clean_name(raw)
        except collection.Invalid:
            continue
        collection.change(cfg, work_ids, name)
        if name in folders(cfg):
            with ingest.exclusive(cfg):
                if copy_in(cfg, conn, work_ids, name)["copied"]:
                    scanner.scan(cfg, conn)
        used.append(name)
    all_works = works.load_all(cfg)
    for work_id in work_ids:
        if work_id in all_works:
            index.update_collections(conn, all_works[work_id])
    return used
