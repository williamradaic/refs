"""library.bib: the whole library as one BibTeX file, kept up to date.

The file is the way out if refs ever breaks or is abandoned. Every paper is an
entry with its PDF (`file`) and its collections (`keywords`), the two fields
JabRef and Zotero both read when they import a .bib: JabRef shows keywords as
groups, Zotero as tags. So courses and projects survive without refs.

It lives in refs' own folder in the library (`<state>/library.bib`, next to
works/ and collections.json). The folder watcher (watch.py) rewrites it a few
seconds after works or collections change, and checks once a minute for
changes that arrived from another Mac. It is written only when its text
changes, so Dropbox stays quiet.

The `file` field uses JabRef's format (`:path:PDF`, the one Zotero and
Mendeley read too) with a path relative to the .bib file, so it holds on
another Mac whose Dropbox is mounted elsewhere.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

from . import bib, collection, index, works

if TYPE_CHECKING:
    import sqlite3

    from .config import Config

NAME = "library.bib"
DEBOUNCE_SECONDS = 3.0       # write this long after the last change...
CHECK_SECONDS = 60.0         # ...and look for changes from elsewhere this often


def path(cfg: Config) -> Path:
    return cfg.absolute(cfg.state_dir) / NAME


def _escape_jabref(text: str) -> str:
    """JabRef's file field separates parts with ":" and ";": escape them."""
    return text.replace("\\", "\\\\").replace(":", "\\:").replace(";", "\\;")


def _library_copy(cfg: Config, locations: list[str]) -> str | None:
    """The PDF the entry points to: the copy in the library (quick-add) folder
    if there is one, else the first by path."""
    if not locations:
        return None
    dump = cfg.dump.strip("/") + "/"
    return next((p for p in sorted(locations) if p.startswith(dump)), sorted(locations)[0])


def render(cfg: Config, conn: sqlite3.Connection) -> str:
    """The text of library.bib: one entry per work, ordered by citekey."""
    all_works = works.load_all(cfg)
    # Each work's PDFs on disk and its collections, from the index.
    located: dict[str, list[str]] = {}
    member_of: dict[str, list[str]] = {}
    for row in conn.execute("SELECT f.work_id, p.path FROM files f JOIN paths p ON p.hash = f.hash "
                            "WHERE f.work_id IS NOT NULL"):
        located.setdefault(row["work_id"], []).append(row["path"])
    import json
    for row in conn.execute("SELECT work_id, collections FROM files "
                            "WHERE work_id IS NOT NULL AND collections IS NOT NULL"):
        names = member_of.setdefault(row["work_id"], [])
        names += [n for n in json.loads(row["collections"]) if n not in names]

    here = path(cfg).parent
    entries = []
    for work in sorted(all_works.values(), key=lambda w: w["citekey"]):
        if "meta" not in work:
            continue
        extra = []
        if (pdf := _library_copy(cfg, located.get(work["id"], []))) is not None:
            relative = os.path.relpath(cfg.absolute(pdf), here)
            extra.append(("file", f":{_escape_jabref(relative)}:PDF"))
        # Index first; explicit memberships of a work the index has not seen yet too.
        names = list(dict.fromkeys([*member_of.get(work["id"], []), *collection.explicit(work)]))
        if names:
            extra.append(("keywords", ", ".join(names)))
        entries.append(bib.entry(work, extra))
    header = ("% library.bib: every paper in refs, written by refs. Do not edit: it is\n"
              "% rewritten whenever the library changes. `keywords` = collections\n"
              "% (courses, projects); `file` = the PDF, relative to this file.\n"
              f"% {len(entries)} entries.\n\n")
    return header + "\n\n".join(entries) + "\n"


def write(cfg: Config, conn: sqlite3.Connection | None = None) -> bool:
    """Write library.bib if its text changed. True if it was written."""
    own = conn is None
    conn = index.connect(cfg) if own else conn
    try:
        text = render(cfg, conn)
    finally:
        if own:
            conn.close()
    target = path(cfg)
    try:
        if target.read_text(encoding="utf-8") == text:
            return False
    except FileNotFoundError:
        pass
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(target)
    return True


def fingerprint(cfg: Config) -> tuple:
    """Something that changes whenever a work file or the registry does: each
    file's name, size and modification time. Cheap (no file is read)."""
    found = []
    for folder in (works.works_dir(cfg),):
        try:
            with os.scandir(folder) as entries:
                for entry in entries:
                    if entry.name.endswith(".json"):
                        info = entry.stat()
                        found.append((entry.name, info.st_size, info.st_mtime_ns))
        except FileNotFoundError:
            pass
    try:
        info = collection.registry_path(cfg).stat()
        found.append(("collections.json", info.st_size, info.st_mtime_ns))
    except FileNotFoundError:
        pass
    return tuple(sorted(found))


class Exporter:
    """Keeps library.bib current from a background thread.

    `poke()` after a change writes it DEBOUNCE_SECONDS later (a burst of edits,
    a batch filing, makes one write). Between pokes it checks the work files
    every CHECK_SECONDS, which catches changes made by the command line or by
    refs on another Mac. Only one process per Mac runs it: the one that
    watches the folders (watch.py).
    """

    def __init__(self, cfg: Config, *, debounce: float = DEBOUNCE_SECONDS,
                 check: float = CHECK_SECONDS, log=None) -> None:
        self.cfg = cfg
        self.debounce, self.check = debounce, check
        self._log = log or (lambda message: None)
        self._wake = threading.Event()
        self._halt = threading.Event()
        self._seen: tuple | None = None
        self.last_written: float | None = None
        self.error: str | None = None

    def poke(self) -> None:
        self._wake.set()

    def start(self) -> None:
        threading.Thread(target=self._loop, name="refs-librarybib", daemon=True).start()

    def stop(self) -> None:
        self._halt.set()
        self._wake.set()

    def run_once(self, force: bool = False) -> bool:
        """Write library.bib if the works or collections changed (or `force`)."""
        seen = fingerprint(self.cfg)
        if not force and seen == self._seen:
            return False
        written = write(self.cfg)
        self._seen = seen
        self.error = None
        if written:
            self.last_written = time.time()
            self._log(f"wrote {self.cfg.state_dir}/{NAME}")
        return written

    def _loop(self) -> None:
        first = True
        while not self._halt.is_set():
            poked = self._wake.wait(0 if first else self.check)
            if self._halt.is_set():
                return
            if poked:
                # Let a burst of changes finish before writing once.
                while self._wake.is_set() and not self._halt.is_set():
                    self._wake.clear()
                    self._halt.wait(self.debounce)
            try:
                # A poke means something changed: write even if the work files
                # look the same (a scan changes which PDF an entry points to).
                self.run_once(force=poked or first)
            except Exception as error:                  # noqa: BLE001 (never stop the thread)
                self.error = f"{type(error).__name__}: {error}"
                self._log(f"library.bib not written: {self.error}")
            first = False
