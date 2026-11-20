"""Follow changes in Dropbox as they happen.

`refs serve` (and so the login service) runs a watcher. macOS reports every
change under the scanned roots (FSEvents, through the `watchdog` package);
the watcher waits until the changes settle, then does what `refs scan`
followed by `refs add` would do:

  1. scan: bring the index in line with the disk (moves, renames, copies,
     deletions are recognised there, see scan.py);
  2. ingest any PDF the pipeline has never read (ingest.py): read and look it
     up, file it under its work and, inside the dump, rename it.

The watching process also keeps library.bib current (librarybib.py).

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
from collections import deque
from datetime import datetime
from pathlib import PurePosixPath
from typing import Callable

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from . import budget, index, ingest, librarybib, notes
from . import scan as scanner
from .config import Config

# Act once no change has been reported for this long (a copy of many files, or
# a browser download, arrives as a burst of events)...
QUIET_SECONDS = 4.0
# ...but at the latest this long after the first change, so a long copy is
# taken in steps instead of waiting for its very end.
MAX_WAIT_SECONDS = 60.0
# How many recent outcomes (a PDF read, skipped or failed; a scan's changes)
# the page's activity list can show.
RECENT = 50


def _log(message: str) -> None:
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} watch: {message}", flush=True)


def notify(title: str, message: str) -> None:
    """A macOS notification. The text goes in as arguments, so quotes are safe."""
    subprocess.run(
        ["osascript", "-e", "on run argv",
         "-e", "display notification (item 2 of argv) with title (item 1 of argv)",
         "-e", "end run", title, message],
        capture_output=True, timeout=10)


def _summary(report: scanner.ScanReport) -> str:
    """What a scan changed, in a few words ("2 new, 1 moved"); empty if nothing."""
    counts = [(len(report.added), "new"), (len(report.copies), "copied"), (len(report.moved), "moved"),
              (len(report.modified), "edited"), (len(report.removed), "gone"),
              (len(report.restored), "back")]
    return ", ".join(f"{n} {word}" for n, word in counts if n)


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
        self.quiet = quiet
        self.notifier = notifier
        self._use(cfg)
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
        # The pass in progress: {"stage": scanning|indexing|reading|filing, "done",
        # "total", "file" (the PDF being read), "since" (when this stage began)},
        # and when the pass began. None between passes.
        self.progress: dict | None = None
        self.pass_started: float | None = None
        self.last_pass_seconds: float | None = None
        # What recent passes did, newest last (see _remember).
        self.recent: deque[dict] = deque(maxlen=RECENT)
        self._entries: dict[str, dict] = {}     # this pass's entries by path, for renames
        # A large batch of unread PDFs waiting for your OK ({"pdfs", "dollars"}),
        # whether you gave it, and whether the monthly limit stopped reading.
        self.held: dict | None = None
        self._approved = False
        self.limit_reached = False
        # Keeps library.bib current; started with the watcher (one per Mac).
        self.exporter: librarybib.Exporter | None = None

    def _use(self, cfg: Config) -> None:
        """Take these settings: what to watch, and what to ignore."""
        self.cfg = cfg
        if getattr(self, "exporter", None) is not None:
            self.exporter.cfg = cfg
        root = str(cfg.dropbox_root)
        # Folders whose changes never matter: the app's own records (written
        # by every pass, so watching them would loop) and excluded paths.
        self._skip = [os.path.join(root, rel) for rel in [cfg.state_dir, *cfg.exclude_paths]]
        # Reading notes (in the Obsidian vault, outside Dropbox): an edited note
        # changes what search finds, so it too starts a pass.
        # None when no vault is set (notes off): then no note path is relevant.
        self._notes = str(notes.folder(cfg)) if notes.folder(cfg) else None

    def _schedule(self) -> None:
        """Ask FSEvents for changes in the watched folders and the notes folder."""
        self._observer.unschedule_all()
        handler = _Handler(self)
        for root in self.cfg.roots:
            top = self.cfg.absolute(root)
            if top.is_dir():
                self._observer.schedule(handler, str(top), recursive=True)
        if self._notes and os.path.isdir(self._notes):
            self._observer.schedule(handler, self._notes, recursive=False)

    def approve(self) -> None:
        """You said yes to the batch the page showed: read it in the next pass."""
        self._approved = True
        self.poke()

    def reconfigure(self, cfg: Config) -> None:
        """Settings changed (the settings page): watch what they now say, and
        run a pass soon. The pass in progress, if any, finishes first: passes
        run one at a time on the watcher's own thread."""
        self._use(cfg)
        if self._observer is not None:
            self._schedule()
            _log(f"settings changed: watching {', '.join(cfg.roots)} (reading new PDFs: {cfg.auto_process})")
            self.poke()

    # --- events ----------------------------------------------------------------------

    def relevant(self, path: str, is_directory: bool) -> bool:
        """Could a change at this path change the index?"""
        if self._notes and path.startswith(self._notes + os.sep):
            name = os.path.basename(path)
            return not is_directory and name.endswith(".md") and not name.startswith(".")
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
        self._schedule()
        self._observer.daemon = True
        self._observer.start()
        _log(f"watching {', '.join(self.cfg.roots)} (reading new PDFs: {self.cfg.auto_process})")

        self._wake.set()                     # first pass now: catch up on changes made while off
        threading.Thread(target=self._loop, name="refs-watch", daemon=True).start()
        self.exporter = librarybib.Exporter(self.cfg, log=_log)
        self.exporter.start()
        return True

    def stop(self) -> None:
        self._halt.set()
        self._wake.set()
        if self.exporter is not None:
            self.exporter.stop()
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
        self._entries = {}
        with ingest.exclusive(cfg):
            self.pass_started = time.time()
            self._stage("scanning", 0, 0)
            conn = index.connect(cfg)
            try:
                report = scanner.scan(cfg, conn, progress=self._scanning)
                self._remember_scan(report)
                pending = ([] if cfg.auto_process == "none" else
                           ingest.unread_files(cfg, conn, dump_only=cfg.auto_process == "dump"))
                # Papers in the quick-add folder (you added them) are read; a large
                # batch anywhere else waits for your OK on the page (budget.py).
                added = [p for p in pending if ingest.in_dump(cfg, p[0])]
                others = [p for p in pending if not ingest.in_dump(cfg, p[0])]
                if len(others) > budget.BULK and not self._approved:
                    self.held = {"pdfs": len(others), "dollars": budget.estimate(len(others))}
                    others = []
                else:
                    self.held = None
                    if others:
                        self._approved = False          # an OK covers one batch
                pending = added + others
                # Reading on its own stops at the monthly limit.
                left = budget.left_this_month(cfg, conn)
                results = (ingest.ingest(cfg, conn, pending, progress=self._reading,
                                         on_result=self._remember, budget_left=left)
                           if pending else [])
                self.limit_reached = any(r.skipped and "reading limit" in r.skipped for r in results)
            finally:
                conn.close()
                self.progress = None
                if self.pass_started is not None:
                    self.last_pass_seconds = round(time.time() - self.pass_started, 1)
                self.pass_started = None
        self.last_pass = datetime.now().isoformat(timespec="seconds")
        self._report(report, results)
        if self.exporter is not None and (report.changes or results):
            self.exporter.poke()             # moved PDFs or new works change library.bib
        return results

    def _stage(self, stage: str, done: int, total: int, file: str | None = None) -> None:
        """Say where the pass is. `since` stays put while the stage does not
        change, so the page can tell how fast it goes and estimate the rest."""
        same = self.progress is not None and self.progress["stage"] == stage
        since = self.progress["since"] if same else time.time()
        self.progress = {"stage": stage, "done": done, "total": total, "file": file, "since": since}

    def _scanning(self, done: int, total: int) -> None:
        # After the last file is hashed, the index and search vectors are
        # rebuilt (the first time, the search model is downloaded).
        self._stage("indexing" if done >= total else "scanning", done, total)

    def _reading(self, done: int, total: int, rel: str = "") -> None:
        # After the last file: filing under works, renaming in the dump, indexing.
        if rel:
            self._stage("reading", done, total, rel)
        else:
            self._stage("filing", done, total)

    def _remember_scan(self, report: scanner.ScanReport) -> None:
        """What the scan found in the folders, for the activity list (if anything)."""
        found = _summary(report)
        if found:
            self.recent.append({"at": time.time(), "outcome": "scan", "path": None,
                                "text": f"folders: {found}", "issues": []})

    def _remember(self, item: ingest.Ingested) -> None:
        """One file's outcome, for the page's activity list, as soon as it is known."""
        if item.skipped:
            outcome = "skipped"
        elif any("failed" in issue or "could not open" in issue for issue in item.issues):
            outcome = "failed"
        elif item.read is not None and item.read.get("status") == "needs-review":
            outcome = "review"
        else:
            outcome = "read"
        entry = {"at": time.time(), "outcome": outcome, "path": item.rel,
                 "text": ingest.headline(item), "issues": list(item.issues)}
        self._entries[item.rel] = entry
        self.recent.append(entry)

    # --- telling you what happened ---------------------------------------------------------

    def _tell(self, title: str, message: str) -> None:
        if self.notifier is not None:
            try:
                self.notifier(title, message)
            except Exception:                # a notification is never worth a failed pass
                pass

    def _report(self, report: scanner.ScanReport, results: list[ingest.Ingested]) -> None:
        summary = _summary(report)
        if summary:
            _log(f"scan: {summary}")
        for item in results:
            if item.renamed_from and (entry := self._entries.get(item.renamed_from)):
                entry["path"], entry["renamed_from"] = item.rel, item.renamed_from
            line = ingest.headline(item)
            if item.renamed_from:
                line += f"  (renamed {PurePosixPath(item.renamed_from).name} -> {PurePosixPath(item.rel).name})"
            _log(f"{item.rel if not item.renamed_from else item.renamed_from}: {line}")
            for issue in item.issues:
                _log(f"    ! {issue}")
        for line in report.finished:
            _log(f"read (from your markup), off the queue: {line}")
        if report.changes or report.finished or any(item.read is not None for item in results):
            self.generation += 1
            self.last_message = self._message(results) or (f"library updated: {summary}" if summary else "")
            if report.finished:              # detected as read while queued: say so
                done = report.finished[0].split(": ", 1)[-1] if len(report.finished) == 1 \
                    else f"{len(report.finished)} papers"
                self.last_message = f"read {done}: off the queue"

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
                "last_pass": self.last_pass, "last_pass_seconds": self.last_pass_seconds,
                "message": self.last_message,
                "progress": self.progress, "pass_started": self.pass_started,
                "pending": self._wake.is_set(), "now": time.time(),
                "roots": list(self.cfg.roots), "auto_process": self.cfg.auto_process,
                "recent": list(reversed(self.recent)),          # newest first
                "held": self.held, "limit_reached": self.limit_reached,
                "library_bib": None if self.exporter is None else
                {"written": self.exporter.last_written, "error": self.exporter.error}}
