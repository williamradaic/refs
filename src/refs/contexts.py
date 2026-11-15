"""Folder awareness: turn a file's path into the course or project it belongs to.

Nothing here is filed by hand. A PDF's context is read off the folders it sits
in, so moving the file in Finder is the same as re-filing it in the app.

How folders become contexts is a list of rules (a "layout"). A rule is a path
pattern, relative to the library folder, made of folder names and placeholders:

    {term}    a term folder: "2026-fall", "Fall 2026", "spring-2025"
    {course}  a course folder: "220a", "1-Econ 201A" (read as "201a")
    {name}    any folder: a project, a journal, a topic
    {area}    any folder, which names the area itself

The first rule that matches a file's folders wins. Its area is the {area}
folder, else the rule's last plain folder name ("coursework" in the first rule
below); its key is the area followed by the placeholder values. Rules come
from `contexts` in config.toml, then the chosen preset (`layout`), then one
generic rule per watched folder: each folder directly inside it is a context.

Example, with the "phd" preset:
    phd/coursework/2026-fall/220a/readings/03-demand-estimation-ii/blp.pdf
    matches "phd/coursework/{term}/{course}"
    -> key "coursework/2026-fall/220a", area "coursework", term "2026-fall",
       slug "220a", section "readings", topic "Demand estimation II", order 3
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import Config

# A term folder, either way round: "2026-fall", "Fall 2026", "spring_2025".
_SEASONS = "fall|autumn|spring|summer|winter"
_TERM_RES = [re.compile(rf"^(?P<year>\d{{4}})[-_ ]?(?P<season>{_SEASONS})$", re.I),
             re.compile(rf"^(?P<season>{_SEASONS})[-_ ]?(?P<year>\d{{4}})$", re.I)]
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
PLACEHOLDERS = {"{term}", "{course}", "{name}", "{area}"}


@dataclass(frozen=True)
class Context:
    """Where a file lives, in the vocabulary of courses and projects."""

    key: str                        # stable identifier, e.g. "research/bets"
    area: str                       # coursework, research, projects, referee, dump, ...
    term: str | None = None         # "2026-fall" (a {term} rule only)
    slug: str | None = None         # "220a", "bets"
    section: str | None = None      # first folder below the context: "readings"
    topic: str | None = None        # "Demand estimation II"
    topic_order: int | None = None  # 3


@dataclass(frozen=True)
class Preset:
    """A ready-made layout: its rules, and the areas the sidebar lists by name
    (in this order; other areas go under "Other")."""

    rules: tuple[str, ...]
    areas: tuple[str, ...]


PRESETS: dict[str, Preset] = {
    # No rules: only the generic rule per watched folder.
    "folders": Preset(rules=(), areas=()),
    # A PhD student's Dropbox: phd/coursework/<term>/<course>, phd/research/<project>,
    # phd/referee/<journal>/<manuscript>, ... (the layout refs was first built on).
    "phd": Preset(
        rules=(
            "phd/coursework/{term}/{course}",
            "phd/coursework/{term}",          # a term holding one course's material directly
            "phd/coursework/{name}",          # e.g. coursework/field_syllabi
            "phd/referee/{name}/{name}",      # journal, then manuscript
            "phd/referee/{name}",
            "phd/research/{name}",
            "phd/projects/{name}",
            "phd/ra/{name}",
            "phd/ta/{name}",
            "phd/tools/{name}",
            "phd/{area}",                     # any other folder in phd/ is an area of its own
            "phd",                            # files directly in phd/
        ),
        areas=("research", "coursework", "projects", "ra", "referee"),
    ),
}


def term_of(folder: str) -> str | None:
    """A term folder's name in one form, "2026-fall"; None if it is not a term."""
    for pattern in _TERM_RES:
        if match := pattern.match(folder.strip()):
            season = match["season"].lower()
            return f"{match['year']}-{'fall' if season == 'autumn' else season}"
    return None


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


# --- Rules -------------------------------------------------------------------

class RuleError(ValueError):
    """A rule in config.toml that cannot work; the message says why."""


@dataclass(frozen=True)
class Rule:
    """One compiled path pattern, e.g. "phd/coursework/{term}/{course}"."""

    text: str
    parts: tuple[str, ...]

    @classmethod
    def parse(cls, text: str) -> Rule:
        parts = tuple(p for p in text.strip().strip("/").split("/") if p)
        if not parts:
            raise RuleError("an empty rule")
        for part in parts:
            if "{" in part and part not in PLACEHOLDERS:
                raise RuleError(f"{text!r}: unknown placeholder {part!r} "
                                f"(use {', '.join(sorted(PLACEHOLDERS))}, each as a whole folder)")
        if "{area}" not in parts and all(p in PLACEHOLDERS for p in parts):
            raise RuleError(f"{text!r} needs a plain folder name or {{area}} to name its area")
        return cls(text, parts)

    @property
    def prefix(self) -> tuple[str, ...]:
        """The plain folder names before the first placeholder (lower case)."""
        out = []
        for part in self.parts:
            if part in PLACEHOLDERS:
                break
            out.append(part.lower())
        return tuple(out)

    def match(self, dirs: tuple[str, ...]) -> tuple[str, str, str | None, str | None] | None:
        """(key, area, term, slug) if the file's leading folders fit, else None."""
        if len(dirs) < len(self.parts):
            return None
        area: str | None = None
        term: str | None = None
        values: list[str] = []        # placeholder values that make up the key
        names: list[str] = []         # {course} and {name} values: the slug
        literal_area: str | None = None
        for part, folder in zip(self.parts, dirs):
            if part == "{term}":
                if (term := term_of(folder)) is None:
                    return None
                values.append(term)
            elif part == "{course}":
                if not _is_course_folder(folder):
                    return None
                values.append(course_slug(folder))
                names.append(values[-1])
            elif part == "{name}":
                values.append(folder)
                names.append(folder)
            elif part == "{area}":
                area = folder
            elif folder.lower() == part.lower():
                # The rule's spelling, so the area matches the sidebar's list
                # even when the folder is "Papers" and the rule says "papers".
                literal_area = part
            else:
                return None
        area = area or literal_area
        return "/".join([area, *values]), area, term, "/".join(names) or None


class Layout:
    """The rules for one library: config.toml's, the preset's, one per root."""

    def __init__(self, rules: list[str], roots: list[str], dump: str,
                 preset: str = "folders") -> None:
        chosen = PRESETS[preset]
        self.rules = [Rule.parse(text) for text in [*rules, *chosen.rules]]
        self.dump = tuple(p for p in dump.strip("/").split("/") if p)
        # Every watched folder ends with the generic rules, tried last: each
        # folder directly inside it is a context, files at its top level belong
        # to the folder itself. Only a watched folder that no earlier rule
        # reaches into is an area the sidebar lists by name.
        covered = [rule.prefix for rule in self.rules]
        generic_areas: list[str] = []
        for root in roots:
            parts = tuple(p for p in root.strip("/").split("/") if p)
            if not parts or parts == self.dump:
                continue
            self.rules += [Rule.parse("/".join([*parts, "{name}"])), Rule.parse("/".join(parts))]
            lowered = tuple(p.lower() for p in parts)
            if not any(prefix and prefix[:len(lowered)] == lowered for prefix in covered):
                generic_areas.append(parts[-1])
        # The sidebar lists these areas by name, in this order: areas named in
        # config.toml's rules, the preset's, then the watched folders'.
        own = [Rule.parse(text) for text in rules]
        named = [part for rule in own if "{area}" not in rule.parts
                 for part in [next(p for p in reversed(rule.parts) if p not in PLACEHOLDERS)]]
        self.areas = list(dict.fromkeys([*named, *chosen.areas, *generic_areas]))

    def derive(self, rel_path: str) -> Context:
        """Work out the context of a file from its Dropbox-relative path."""
        dirs = PurePosixPath(rel_path).parts[:-1]   # folders only, file name dropped

        # --- Step 1: find the context and how many folders it uses up -------
        key, area, term, slug = "other", "other", None, None
        used = 0   # number of leading folders that belong to the context itself

        if self.dump and dirs[:len(self.dump)] == self.dump:
            key, area, used = "dump", "dump", len(self.dump)
        else:
            for rule in self.rules:
                if (found := rule.match(dirs)) is not None:
                    (key, area, term, slug), used = found, len(rule.parts)
                    break
            else:
                if dirs:                                # outside every rule
                    area = key = dirs[0]
                    used = 1

        # --- Step 2: read section and topic from the folders below the context
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


def layout_for(cfg: Config) -> Layout:
    """The layout config.toml describes (`layout`, `contexts`, `roots`, `dump`)."""
    return Layout(cfg.contexts, cfg.roots, cfg.dump, cfg.layout)
