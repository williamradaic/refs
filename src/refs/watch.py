"""Follow changes in Dropbox as they happen.

`refs serve` (and so the login service) runs a watcher. macOS reports every
change under the scanned roots (FSEvents, through the `watchdog` package);
the watcher waits until the changes settle, then does what `refs scan`
followed by `refs add` would do:

  1. scan: bring the index in line with the disk (moves, renames, copies,
     deletions are recognised there, see scan.py);
  2. ingest any PDF the pipeline has never read (ingest.py): read and look it
     up, file it under its work and, inside the dump, rename it.

When the watcher starts it does one such pass straight away, which catches
whatever changed while refs was not running.

Which new files are read is the `auto_process` setting (config.py). Notifications
appear for papers added through the dump and for anything that went wrong;
everything is also written to the log (~/Library/Logs/refs.log for the service).
"""

from __future__ import annotations

import fcntl
import os
import subprocess
import threading
import time
import traceback
from datetime import datetime
from pathlib import PurePosixPath
from typing import Callable

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from . import index, ingest
from . import scan as scanner
from .config import Config

# Act once no change has been reported for this long (a copy of many files, or
# a browser download, arrives as a burst of events)...
QUIET_SECONDS = 4.0
# ...but at the latest this long after the first change, so a long copy is
# taken in steps instead of waiting for its very end.
MAX_WAIT_SECONDS = 60.0


def _log(message: str) -> None:
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} watch: {message}", flush=True)


def notify(title: str, message: str) -> None:
    """A macOS notification. The text goes in as arguments, so quotes are safe."""
    subprocess.run(
        ["osascript", "-e", "on run argv",
         "-e", "display notification (item 2 of argv) with title (item 1 of argv)",
         "-e", "end run", title, message],
        capture_output=True, timeout=10)


class _Handler(FileSystemEventHandler):
    """Passes on the events that can matter to the index; ignores the rest."""

    def __init__(self, watcher: "Watcher") -> None:
        self.watcher = watcher

    def on_any_event(self, event: FileSystemEvent) -> None:
        if event.event_type in {"opened", "closed", "closed_no_write"}:
            return
        # A folder changes ("modified") whenever anything inside it does; only
        # a folder appearing, vanishing or moving can bring or take PDFs.
        if event.is_directory and event.event_type not in {"created", "deleted", "moved"}:
            return
        paths = [os.fsdecode(event.src_path), os.fsdecode(getattr(event, "dest_path", "") or "")]
        if any(self.watcher.relevant(p, event.is_directory) for p in paths if p):
            self.watcher.poke()


class Watcher:
    """Runs a scan-and-ingest pass whenever the watched folders settle after a change."""

    def __init__(self, cfg: Config, *, quiet: float = QUIET_SECONDS,
                 notifier: Callable[[str, str], None] | None = notify) -> None:
        self.cfg = cfg
        self.quiet = quiet
        self.notifier = notifier
        root = str(cfg.dropbox_root)
        # Folders whose changes never matter: the app's own records (written
        # by every pass, so watching them would loop) and excluded paths.
        self._skip = [os.path.join(root, rel) for rel in [cfg.state_dir, *cfg.exclude_paths]]
        self._wake = threading.Event()
        self._halt = threading.Event()
        self._first_event = self._last_event = 0.0
        self._observer = None
        self._lock_file = None
        self._warned_no_key = False
        # Read by the web page to know when to reload the list.
        self.generation = 0
        self.last_pass: str | None = None
        self.last_message = ""

    # --- events ----------------------------------------------------------------------

    def relevant(self, path: str, is_directory: bool) -> bool:
        """Could a change at this path change the index?"""
        if any(path == skip or path.startswith(skip + os.sep) for skip in self._skip):
            return False
        parts = PurePosixPath(path).parts
        if any(part.lower() in self.cfg.ignore_dirs for part in parts):
            return False
        if is_directory:
            return True
        name = parts[-1] if parts else ""
        return name.lower().endswith(".pdf") and not name.startswith("._")

    def poke(self) -> None:
        """Note a change; a pass follows once things are quiet."""
        now = time.monotonic()
        if not self._wake.is_set():
            self._first_event = now
        self._last_event = now
        self._wake.set()

    # --- running ---------------------------------------------------------------------

    def start(self) -> bool:
        """Start watching in the background. False if another refs already watches."""
        # One watcher per Mac: a second `refs serve` (or `refs watch`) stays quiet.
        lock_path = self.cfg.index_db.parent / "watch.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock_file = lock_path.open("w")
        try:
            fcntl.flock(self._lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self._lock_file.close()
            self._lock_file = None
            _log("another refs process is already watching; this one will not")
            return False

        self._observer = Observer()
        handler = _Handler(self)
        for root in self.cfg.roots:
            top = self.cfg.absolute(root)
            if top.is_dir():
                self._observer.schedule(handler, str(top), recursive=True)
        self._observer.daemon = True
        self._observer.start()
        _log(f"watching {', '.join(self.cfg.roots)} (reading new PDFs: {self.cfg.auto_process})")

        self._wake.set()                     # first pass now: catch up on changes made while off
        threading.Thread(target=self._loop, name="refs-watch", daemon=True).start()
        return True

    def stop(self) -> None:
        self._halt.set()
        self._wake.set()
        if self._observer is not None:
            self._observer.stop()
        if self._lock_file is not None:
            self._lock_file.close()          # closing releases the lock

    def _settle(self) -> None:
        """Wait until no change has arrived for `quiet` seconds (or MAX_WAIT has passed)."""
        while not self._halt.is_set():
            now = time.monotonic()
            quiet_for, waited = now - self._last_event, now - self._first_event
            if quiet_for >= self.quiet or waited >= MAX_WAIT_SECONDS:
                return
            self._halt.wait(min(self.quiet - quiet_for, 1.0) + 0.05)

    def _loop(self) -> None:
        while not self._halt.is_set():
            self._wake.wait()
            self._settle()
            if self._halt.is_set():
                return
            # Clear before the pass: a change that arrives during it sets the
            # flag again and so gets a pass of its own afterwards.
            self._wake.clear()
            try:
                self.run_pass()
            except Exception:
                _log("pass failed:\n" + traceback.format_exc())
                self._tell("refs: watcher error", "A pass failed; details in ~/Library/Logs/refs.log.")

    def run_pass(self) -> list[ingest.Ingested]:
        """One scan-and-ingest pass. Public so it can be run directly (and tested)."""
        cfg = self.cfg
        with ingest.exclusive(cfg):
            conn = index.connect(cfg)
            try:
                report = scanner.scan(cfg, conn)
                pending = ([] if cfg.auto_process == "none" else
                           ingest.unread_files(cfg, conn, dump_only=cfg.auto_process == "dump"))
                results = ingest.ingest(cfg, conn, pending) if pending else []
            finally:
                conn.close()
        self.last_pass = datetime.now().isoformat(timespec="seconds")
        self._report(report, results)
        return results

    # --- telling you what happened ---------------------------------------------------------

    def _tell(self, title: str, message: str) -> None:
        if self.notifier is not None:
            try:
                self.notifier(title, message)
            except Exception:                # a notification is never worth a failed pass
                pass

    def _report(self, report: scanner.ScanReport, results: list[ingest.Ingested]) -> None:
        moved = len(report.moved)
        counts = [(len(report.added), "new"), (len(report.copies), "copied"), (moved, "moved"),
                  (len(report.modified), "edited"), (len(report.removed), "gone"),
                  (len(report.restored), "back")]
        summary = ", ".join(f"{n} {word}" for n, word in counts if n)
        if summary:
            _log(f"scan: {summary}")
        for item in results:
            line = ingest.headline(item)
            if item.renamed_from:
                line += f"  (renamed {PurePosixPath(item.renamed_from).name} -> {PurePosixPath(item.rel).name})"
            _log(f"{item.rel if not item.renamed_from else item.renamed_from}: {line}")
            for issue in item.issues:
                _log(f"    ! {issue}")
        if report.changes or any(item.read is not None for item in results):
            self.generation += 1
            self.last_message = self._message(results) or (f"library updated: {summary}" if summary else "")

        # Notifications: every paper that came in through the dump, and problems.
        for item in results:
            failed = any("failed" in issue or "could not open" in issue for issue in item.issues)
            if item.processed_now and ingest.in_dump(self.cfg, item.rel):
                title = "refs: needs review" if item.read["status"] == "needs-review" or failed else "refs: added"
                self._tell(title, ingest.headline(item))
            elif failed:
                self._tell("refs: could not read a PDF", f"{item.rel}: {item.issues[0]}")
        if any(item.skipped and "API key" in item.skipped for item in results) and not self._warned_no_key:
            self._warned_no_key = True
            self._tell("refs: no API key", "New PDFs are indexed but not read. Run `refs setup-key`.")

    @staticmethod
    def _message(results: list[ingest.Ingested]) -> str:
        """A short line for the web page about what was just read."""
        read = [item for item in results if item.processed_now]
        if not read:
            return ""
        if len(read) == 1:
            return f"added {ingest.headline(read[0])}"
        return f"read {len(read)} new PDFs"

    def status(self) -> dict:
        return {"watching": self._observer is not None, "generation": self.generation,
                "last_pass": self.last_pass, "message": self.last_message}
