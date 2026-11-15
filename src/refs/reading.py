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

import pymupdf

pymupdf.TOOLS.mupdf_display_errors(False)

RANK = {"unread": 0, "reading": 1, "read": 2}

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
    try:
        doc = pymupdf.open(path)
    except Exception:                                   # noqa: BLE001 (damaged, encrypted, not a PDF)
        return 0, 0, 0
    marked_pages = marks = 0
    try:
        for page in doc:
            here = sum(1 for a in page.annots() or []
                       if a.type[1] in MARKUP
                       and (a.info.get("title") or "").strip().lower() in authors)
            marks += here
            marked_pages += here > 0
        return doc.page_count, marked_pages, marks
    except Exception:                                   # noqa: BLE001 (broken page tree)
        return doc.page_count, marked_pages, marks
    finally:
        doc.close()


def marks_by_author(path: Path) -> dict[str, int]:
    """How many highlights, underlines, notes and ink marks each author left in
    one PDF, by the name as written ("" = no author). For the settings page,
    which lets you pick the names that are you."""
    found: dict[str, int] = {}
    try:
        doc = pymupdf.open(path)
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


def set_status(work: dict, status: str) -> None:
    """Your own choice of read status. Reading a paper takes it off the queue."""
    if status not in RANK:
        raise ValueError(f"read status must be one of {', '.join(RANK)}, not {status!r}")
    work["user"]["read_status"] = status
    work["user"]["read_set"] = now()
    if status == "read":
        work["user"].pop("queued", None)


# --- 3. The queue ---------------------------------------------------------------------------

def set_queued(work: dict, on: bool) -> None:
    """Put a work on the queue (stamped with the time, which orders the queue) or take it off."""
    if on:
        work["user"].setdefault("queued", now())
    else:
        work["user"].pop("queued", None)
