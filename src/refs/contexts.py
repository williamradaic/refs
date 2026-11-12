"""Folder awareness: turn a file's path into the course or project it belongs to.

Nothing here is filed by hand. A PDF's context is read off the folders it sits
in, so moving the file in Finder is the same as re-filing it in the app.

Example:
    phd/coursework/2026-fall/220a/readings/03-demand-estimation-ii/blp.pdf
    -> key "coursework/2026-fall/220a", area "coursework", term "2026-fall",
       slug "220a", section "readings", topic "Demand estimation II", order 3
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath

# "2026-fall", "2025-summer"
TERM_RE = re.compile(r"^\d{4}-(fall|spring|summer|winter)$")
# A course code after cleaning: "220a", "244", "c110".
COURSE_CODE_RE = re.compile(r"^[a-z]{0,2}\d{2,3}[a-z]{0,2}$")
# "03-demand-estimation-ii" -> order 3, name "demand-estimation-ii"
ORDERED_RE = re.compile(r"^(\d{1,3})[-_ .]+(.+)$")

# Folder names that are material inside a course, not a course of their own.
# Needed for terms that hold a single course directly (e.g. 2025-summer).
GENERIC_SECTIONS = {
    "handouts", "readings", "slides", "lectures", "notes", "exams", "psets",
    "assignments", "syllabus", "sections", "solutions", "other", "extra", "pdfs",
}
ROMAN = {"ii", "iii", "iv", "vi", "vii", "viii", "ix"}


@dataclass(frozen=True)
class Context:
    """Where a file lives, in the vocabulary of courses and projects."""

    key: str                        # stable identifier, e.g. "research/bets"
    area: str                       # coursework, research, projects, referee, dump, ...
    term: str | None = None         # "2026-fall" (coursework only)
    slug: str | None = None         # "220a", "bets"
    section: str | None = None      # first folder below the context: "readings"
    topic: str | None = None        # "Demand estimation II"
    topic_order: int | None = None  # 3


def course_slug(folder: str) -> str:
    """Clean a course folder name: "1-Econ 201A" -> "201a", "220a" -> "220a"."""
    name = folder.strip()
    ordered = ORDERED_RE.match(name)
    if ordered and not COURSE_CODE_RE.match(name.lower()):
        name = ordered.group(2)          # drop a leading "1-" used for sorting
    name = re.sub(r"^econ(omics)?\s+", "", name, flags=re.IGNORECASE)
    return re.sub(r"\s+", "-", name.strip().lower())


def _is_course_folder(folder: str) -> bool:
    """Decide whether a folder directly inside a term is a course."""
    slug = course_slug(folder)
    if COURSE_CODE_RE.match(slug):
        return True
    # Names with spaces ("Lecture Notes") or generic words ("Handouts") are
    # material of a single-course term, not courses.
    return " " not in folder.strip() and slug not in GENERIC_SECTIONS


def pretty_topic(folder: str) -> tuple[str, int | None]:
    """"03-demand-estimation-ii" -> ("Demand estimation II", 3)."""
    order: int | None = None
    name = folder
    ordered = ORDERED_RE.match(folder)
    if ordered:
        order, name = int(ordered.group(1)), ordered.group(2)
    words = re.split(r"[-_\s]+", name.strip())
    # Roman numerals are upper-cased; "i" only when it is the last word, since
    # elsewhere it is more likely part of a phrase.
    words = [
        w.upper() if (w.lower() in ROMAN or (w.lower() == "i" and i == len(words) - 1 and i > 0)) else w
        for i, w in enumerate(words)
    ]
    label = " ".join(w for w in words if w)
    return (label[:1].upper() + label[1:] if label else folder), order


def derive(rel_path: str, dump: str = "papers-dump") -> Context:
    """Work out the context of a file from its Dropbox-relative path."""
    dirs = PurePosixPath(rel_path).parts[:-1]   # folders only, file name dropped

    # --- Step 1: find the context and how many folders it uses up -----------
    key, area, term, slug = "other", "other", None, None
    used = 0   # number of leading folders that belong to the context itself

    if dirs and dirs[0] == dump:
        key, area, used = "dump", "dump", 1

    elif dirs and dirs[0] == "phd":
        rest = dirs[1:]
        area = rest[0] if rest else "phd"
        key, used = area, min(len(dirs), 2)

        if area == "coursework" and len(rest) >= 2:
            if TERM_RE.match(rest[1]):
                term = rest[1]
                key, used = f"coursework/{term}", 3
                if len(rest) >= 3 and _is_course_folder(rest[2]):
                    slug = course_slug(rest[2])
                    key, used = f"coursework/{term}/{slug}", 4
            else:                                   # e.g. coursework/field_syllabi
                slug = rest[1]
                key, used = f"coursework/{slug}", 3

        elif area == "referee" and len(rest) >= 2:  # referee/<journal>/<manuscript>
            slug = "/".join(rest[1:3])
            key, used = f"referee/{slug}", 1 + min(len(rest), 3)

        elif area in {"research", "projects", "ra", "ta", "tools"} and len(rest) >= 2:
            slug = rest[1]
            key, used = f"{area}/{slug}", 3

    elif dirs:                                      # some other scanned root
        area = key = dirs[0]
        used = 1

    # --- Step 2: read section and topic from the folders below the context --
    below = dirs[used:]
    section = below[0] if below else None

    topic, topic_order = None, None
    for i, folder in enumerate(below):
        # A "readings" folder (also "Required Readings") groups papers by topic:
        # the folder right under it names the topic.
        if "reading" in folder.lower() and i + 1 < len(below):
            topic, topic_order = pretty_topic(below[i + 1])
            break

    return Context(key, area, term, slug, section, topic, topic_order)
