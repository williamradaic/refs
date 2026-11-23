"""What you have read, and what you mean to read next.

Two things live here:

1. **Detecting reading from your markup.** A PDF you highlighted, underlined
   or wrote on carries those marks inside the file. Counting the *pages* that
   hold your marks (not the marks themselves: you may highlight a paragraph in
   ten pieces) says how far through the paper you went. Only marks whose
   author is you count (`annotation_authors` in config.py), so an instructor's
   highlights on a course reading do not make it "read".

2. **The queue**: papers you want to read soon, in the order you queued them.

Read status has three values, in this order: unread < reading < read. What you
set by hand always wins. Detection only ever raises a status, and never
overrides one you set (marking a detected paper unread keeps it unread).
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

_pymupdf = None


def _mupdf():
    """Load PyMuPDF lazily so quick CLI commands never pay its import cost."""
    global _pymupdf
    if _pymupdf is None:
        import pymupdf
        pymupdf.TOOLS.mupdf_display_errors(False)
        _pymupdf = pymupdf
    return _pymupdf


RANK = {"unread": 0, "reading": 1, "skimmed": 2, "read": 3}
ORDER = ("unread", "reading", "skimmed", "read")

# Annotation types a reader adds. Links, form fields and popups come with the
# file from the publisher, so they are not counted.
MARKUP = {"Highlight", "Underline", "StrikeOut", "Squiggly", "Text", "FreeText", "Ink",
          "Caret", "Square", "Circle", "Line", "Polygon", "PolyLine"}

# The rule (calibrated on the library in November 2026: papers you read have
# your marks on 15-40 pages; skimmed ones on 1-5).
READ_SHARE = 0.30        # share of pages with your marks for "read"
READ_MIN_PAGES = 5       # ... and at least this many pages (half the pages if shorter)
STARTED_MIN_PAGES = 2    # "reading": marks on at least this many pages
LONG_PAGES = 150         # a book: marks make it "reading", never "read"


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --- 1. Counting your marks in a PDF ------------------------------------------------------

def count_marks(path: Path, authors: set[str]) -> tuple[int, int, int]:
    """(pages, pages with your marks, your marks) of one PDF.

    `authors` are the annotation author names that mean you, lowercase; "" is
    a mark with no author. A file that cannot be opened counts as (0, 0, 0),
    so it is not tried again until its content changes. About 4 ms a file.
    """
    found = survey(path, authors)
    return found["pages"], found["marked_pages"], found["marks"]


def pdf_date(text: str | None) -> str | None:
    """A PDF date ("D:20261112093000-08'00'") as an ISO day-and-time, or None."""
    digits = "".join(ch for ch in (text or "").removeprefix("D:")[:14] if ch.isdigit())
    if len(digits) < 8:
        return None
    digits = digits.ljust(14, "0")
    try:
        return datetime(int(digits[:4]), int(digits[4:6]), int(digits[6:8]), int(digits[8:10]),
                        int(digits[10:12]), int(digits[12:14])).isoformat(timespec="seconds")
    except ValueError:
        return None


def survey(path: Path, authors: set[str]) -> dict:
    """Everything refs keeps about the marks in one PDF: `pages`, and for your
    marks (`authors`, as in count_marks) `marked_pages` and `marks`; for
    everyone's, `by_author` ({name as written: marks}) and `latest` (the
    newest mark's date, ISO, None when the marks carry no date)."""
    found = {"pages": 0, "marked_pages": 0, "marks": 0, "by_author": {}, "latest": None}
    try:
        doc = _mupdf().open(path)
    except Exception:                                   # noqa: BLE001 (damaged, encrypted, not a PDF)
        return found
    try:
        found["pages"] = doc.page_count
        for page in doc:
            here = 0
            for a in page.annots() or []:
                if a.type[1] not in MARKUP:
                    continue
                name = (a.info.get("title") or "").strip()
                found["by_author"][name] = found["by_author"].get(name, 0) + 1
                when = pdf_date(a.info.get("modDate")) or pdf_date(a.info.get("creationDate"))
                if when and (found["latest"] is None or when > found["latest"]):
                    found["latest"] = when
                here += name.lower() in authors
            found["marks"] += here
            found["marked_pages"] += here > 0
    except Exception:                                   # noqa: BLE001 (broken page tree)
        pass
    finally:
        doc.close()
    return found


def marks_by_author(path: Path) -> dict[str, int]:
    """How many highlights, underlines, notes and ink marks each author left in
    one PDF, by the name as written ("" = no author). For the settings page,
    which lets you pick the names that are you."""
    found: dict[str, int] = {}
    try:
        doc = _mupdf().open(path)
    except Exception:                                   # noqa: BLE001 (damaged, encrypted, not a PDF)
        return found
    try:
        for page in doc:
            for a in page.annots() or []:
                if a.type[1] in MARKUP:
                    name = (a.info.get("title") or "").strip()
                    found[name] = found.get(name, 0) + 1
    except Exception:                                   # noqa: BLE001 (broken page tree)
        pass
    finally:
        doc.close()
    return found


def detect(pages: int, marked_pages: int) -> str | None:
    """The read status your marks suggest: "read", "reading", or None (no sign)."""
    if not pages or marked_pages < STARTED_MIN_PAGES:
        return None
    if pages > LONG_PAGES:
        return "reading"
    enough = min(READ_MIN_PAGES, (pages + 1) // 2)
    if marked_pages >= enough and marked_pages / pages >= READ_SHARE:
        return "read"
    return "reading"


# --- 2. A work's read status ---------------------------------------------------------------

def effective(work: dict) -> tuple[str, str | None]:
    """(status, who decided it): "you", "detected", or None for the default unread."""
    user = work.get("user") or {}
    mine = user.get("read_status") or "unread"
    if user.get("read_set"):                            # you chose it: that is the answer
        return mine, "you"
    detected = (work.get("read_auto") or {}).get("status")
    if detected and RANK[detected] > RANK[mine]:
        return detected, "detected"
    return mine, ("you" if mine != "unread" else None)


def apply_detection(work: dict, evidence: tuple[int, int, int] | None) -> bool:
    """Record what the work's best file says about your reading.

    `evidence` is (pages, pages with your marks, marks) of the work's most-read
    file on disk. The detected status never goes down (a deleted copy or a
    stricter rule does not un-read a paper). Returns True when the paper just
    became "read" by detection and so left the queue.
    """
    before = work.get("read_auto") or {}
    old = before.get("status")
    status = detect(evidence[0], evidence[1]) if evidence else None
    if status is None:
        return False
    if old and (RANK[status] < RANK[old]
                or (status == old and evidence[1] <= before.get("pages", 0))):
        return False                                    # lower, or nothing new
    work["read_auto"] = {"status": status, "pages": evidence[1], "of": evidence[0],
                         "marks": evidence[2],
                         # when detection first reached this status
                         "since": before.get("since") if status == old else now()}
    user = work["user"]
    finished = (status == "read" and old != "read"
                and user.get("queued") and not user.get("read_set"))
    if finished:
        user.pop("queued", None)
    return bool(finished)


def set_status(work: dict, status: str, *, context: str | None = None, note: str | None = None) -> None:
    """Your own choice of read status. Reading a paper takes it off the queue
    and logs a reading entry for `skimmed` and `read`."""
    if status not in RANK:
        raise ValueError(f"read status must be one of {', '.join(RANK)}, not {status!r}")
    user = work["user"]
    user["read_status"] = status
    stamp = now()
    user["read_set"] = stamp
    if status == "read":
        for k in ("queued", "queue_note", "queue_for", "queue_priority"):
            user.pop(k, None)
    if status in {"skimmed", "read"}:
        log_read(work, status, context=context, note=note, at=stamp[:10])


def log_read(work: dict, status: str = "read", *, context: str | None = None,
             note: str | None = None, at: str | None = None) -> dict:
    """Append a reading event (`at`, `status`, optional `context` and `note`)
    to `work["user"]["reads"]`. Avoids duplicate same-day identical entries."""
    if status not in {"reading", "skimmed", "read"}:
        raise ValueError(f"log status must be reading, skimmed or read, not {status!r}")
    day = (at or now())[:10]
    entry: dict = {"at": day, "status": status}
    if context:
        entry["context"] = context.strip().lstrip("#")
    if note and note.strip():
        entry["note"] = note.strip()
    reads = list(work["user"].get("reads") or [])
    if reads and reads[-1].get("at") == day and reads[-1].get("status") == status \
            and reads[-1].get("context") == entry.get("context"):
        if entry.get("note"):
            reads[-1]["note"] = entry["note"]
    else:
        reads.append(entry)
    work["user"]["reads"] = reads
    return entry


def last_read_at(work: dict | None) -> str | None:
    """The latest date/time the paper was read or skimmed, if any."""
    if not work:
        return None
    user = work.get("user") or {}
    reads = user.get("reads") or []
    if reads:
        return max((r.get("at") or "" for r in reads), default="") or None
    status, source = effective(work)
    if status in {"skimmed", "read"}:
        if source == "you" and user.get("read_set"):
            return user["read_set"][:10]
        if source == "detected" and (work.get("read_auto") or {}).get("since"):
            return work["read_auto"]["since"][:10]
    return None


# --- 3. The queue ---------------------------------------------------------------------------

def set_queued(work: dict, on: bool, *, note: str | None = None,
               for_collection: str | None = None, priority: str | None = None) -> None:
    """Put a work on the queue (stamped with the time, which orders the queue) or take it off."""
    user = work["user"]
    if on:
        user.setdefault("queued", now())
        update_queue(work, note=note, for_collection=for_collection, priority=priority)
    else:
        for k in ("queued", "queue_note", "queue_for", "queue_priority"):
            user.pop(k, None)


def update_queue(work: dict, *, note: str | None = None,
                 for_collection: str | None = None, priority: str | None = None) -> None:
    """Update optional metadata on a queued work (a `None` argument leaves that field alone;
    an empty string clears it)."""
    user = work["user"]
    if not user.get("queued"):
        return
    if note is not None:
        clean = note.strip()
        if clean:
            user["queue_note"] = clean
        else:
            user.pop("queue_note", None)
    if for_collection is not None:
        clean = for_collection.strip().lstrip("#")
        if clean:
            user["queue_for"] = clean
        else:
            user.pop("queue_for", None)
    if priority is not None:
        clean = priority.strip().lower()
        if clean in {"next", "high"}:
            user["queue_priority"] = "next"
        else:
            user.pop("queue_priority", None)
