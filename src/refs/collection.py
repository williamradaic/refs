"""Collections: courses and projects as sets of papers, not as folders.

A collection is a name such as `220a` or `bets`. A paper belongs to any number
of them, whichever folder its PDF is in. Sub-collections use a slash
(`219a/public-economics`); a parent includes everything in its children.

Two kinds of membership:

  explicit  stored on the work, `user.collections` (works.py): you put the
            paper there (the page, `refs collection add`, a Zotero import).
  implicit  read off the folders (contexts.py): a PDF in a watched course or
            project folder, such as a shared one, is in that collection; it
            cannot be removed except by moving the file.

The registry, `<state>/collections.json`, describes collections: kind (course
or project), term, a folder, whether that folder is shared with coauthors, a
project .bib file. A collection does not have to be in the registry to have
members (a Zotero collection, a course read off the folders); registering it
only adds those details.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from . import contexts, records

if TYPE_CHECKING:
    import sqlite3

    from .config import Config

KINDS = ("course", "project")
# Areas whose folders are courses (the rest, research/projects/ra..., are projects).
COURSE_AREAS = {"coursework", "courses", "classes", "teaching", "ta"}
# Folder-derived collections are not made for these areas: the quick-add folder
# is the library itself, and "other" is whatever no rule recognised.
NO_COLLECTION_AREAS = {"dump", "other"}
# One part of a name: lowercase letters, digits, dot, underscore, hyphen.
_PART = r"[a-z0-9][a-z0-9._-]*"
NAME_RE = re.compile(rf"^{_PART}(/{_PART})*$")


class Invalid(ValueError):
    """A collection name or setting that cannot be used; the message says why."""


# --- Names ------------------------------------------------------------------------------

def clean_name(text: str) -> str:
    """A collection name as stored: "#Econ 220A" -> "econ-220a", "219A / PF" -> "219a/pf"."""
    parts = [re.sub(r"[^a-z0-9._-]+", "-", part.strip().lower()).strip("-")
             for part in text.strip().lstrip("#").split("/")]
    name = "/".join(parts)
    if not NAME_RE.match(name):
        raise Invalid(f"{text!r} cannot be a collection name (use letters, digits, - and /)")
    return name


def matches(name: str, wanted: str) -> bool:
    """Is `name` the collection `wanted` or one of its sub-collections?"""
    return name == wanted or name.startswith(wanted + "/")


def parent(name: str) -> str | None:
    return name.rsplit("/", 1)[0] if "/" in name else None


def guess_kind(name: str, registry: dict[str, dict]) -> str:
    """A new collection's kind: its parent's, else course for a course code (220a)."""
    top = name.split("/")[0]
    if top in registry:
        return registry[top]["kind"]
    return "course" if contexts.COURSE_CODE_RE.match(top) else "project"


# --- The registry -----------------------------------------------------------------------

def registry_path(cfg: Config) -> Path:
    return cfg.absolute(cfg.state_dir) / "collections.json"


def load(cfg: Config) -> dict[str, dict]:
    """Every registered collection, by name."""
    data = records.read_json(registry_path(cfg)) or {}
    return dict(data.get("collections") or {})


def save(cfg: Config, registry: dict[str, dict]) -> None:
    ordered = {name: registry[name] for name in sorted(registry)}
    records.write_json(registry_path(cfg), {"collections": ordered})


def register(cfg: Config, name: str, *, kind: str | None = None, term: str | None = None,
             folder: str | None = None, shared: bool | None = None, bib_file: str | None = None,
             order: int | None = None, label: str | None = None,
             registry: dict[str, dict] | None = None) -> dict:
    """Add a collection to the registry, or update the details given for one.

    Returns its entry. A detail left as None stays as it was. `folder` is
    relative to the library (Dropbox) root. `order` and `label` place a part
    in its course's reading list ("03-demand-estimation-ii" -> 3, "Demand
    estimation II"); for a project, `order` is its place in the
    sidebar. `registry` lets a caller register several and save once.
    """
    own = registry is None
    registry = load(cfg) if own else registry
    name = clean_name(name)
    if kind is not None and kind not in KINDS:
        raise Invalid(f"kind must be one of {', '.join(KINDS)}, not {kind!r}")
    if term is not None and term != "":
        term = contexts.term_of(term) or _bad(f"{term!r} is not a term (say 2026-fall or Fall 2026)")
    if folder:
        folder = folder.strip()
        if folder.startswith(("~", "/")) or ".." in folder.split("/"):
            raise Invalid("the folder is given relative to the library folder, e.g. phd/research/bets")
        folder = folder.strip("/")
    entry = registry.get(name) or {
        "kind": kind or guess_kind(name, registry), "term": None, "folder": None,
        "shared": False, "bib_file": None,
        "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    if kind:
        entry["kind"] = kind
    if term is not None:
        entry["term"] = term or None
    if folder is not None:
        entry["folder"] = folder or None
    if shared is not None:
        entry["shared"] = bool(shared)
    if bib_file is not None:
        entry["bib_file"] = bib_file or None
    if order is not None:
        entry["order"] = order
    if label is not None:
        entry["label"] = label or None
    registry[name] = entry
    if own:
        save(cfg, registry)
    return entry


def bib_path(cfg: Config, entry: dict) -> Path | None:
    """A collection's .bib file as a path on this Mac: "~/..." or "/..." as
    given, anything else inside the library (Dropbox) folder."""
    text = (entry or {}).get("bib_file")
    if not text:
        return None
    return Path(text).expanduser() if text.startswith(("~", "/")) else cfg.absolute(text)


def _bad(message: str):
    raise Invalid(message)


# --- Folder-derived (implicit) collections ---------------------------------------------

def implicit(ctx: contexts.Context) -> tuple[str, str, str | None] | None:
    """The collection a folder context stands for: (name, kind, term), or None.

    phd/coursework/2026-fall/220a -> ("220a", "course", "2026-fall");
    phd/research/bets -> ("bets", "project", None). A term folder that holds a
    single course's files directly is named after the term ("2025-summer").
    """
    if ctx.area in NO_COLLECTION_AREAS:
        return None
    raw = ctx.slug or ctx.term
    if not raw:
        return None
    try:
        name = clean_name(raw)
    except Invalid:
        return None
    kind = "course" if ctx.term or ctx.area.lower() in COURSE_AREAS else "project"
    return name, kind, ctx.term


def topic_part(topic: str | None) -> str | None:
    """A topic folder's name as a sub-collection part: "03-demand-estimation-ii"
    -> "demand-estimation-ii", "9 & 10 - Application to Public Economics" ->
    "application-to-public-economics". Leading numbers (the order) go."""
    if not topic:
        return None
    text = re.sub(r"^[\d\s&,.+_-]+", "", topic.strip()) or topic
    try:
        return clean_name(text.replace("/", "-"))
    except Invalid:
        return None


def suggested(ctx: contexts.Context) -> str | None:
    """The collection a PDF at this place belongs in: its course or project,
    and for a course, inside a readings/<topic> folder, that topic as a part
    ("220a/demand-estimation-ii"). A project's readings folders (bets/readings/
    lit) are not topics."""
    found = implicit(ctx)
    if found is None:
        return None
    part = topic_part(ctx.topic_folder) if found[1] == "course" else None
    return f"{found[0]}/{part}" if part else found[0]


# --- Membership on works ------------------------------------------------------------------

def explicit(work: dict) -> list[str]:
    """The collections you put a work in, as names refs uses ("Suburbia" ->
    "suburbia"). Names written elsewhere (a Zotero import before it cleaned
    them, an edited file) are read cleaned, so #suburbia, rename and delete
    find them; put and take write the cleaned list back. A name that cannot
    be cleaned is kept as written."""
    return cleaned((work.get("user") or {}).get("collections") or [])


def cleaned(names: list[str]) -> list[str]:
    """Names as refs uses them, in order, without repeats (see explicit)."""
    out = []
    for raw in names:
        try:
            out.append(clean_name(raw))
        except Invalid:
            out.append(raw)
    return list(dict.fromkeys(out))


def put(work: dict, name: str) -> bool:
    """Add a work to a collection. True if that changed anything."""
    have = explicit(work)
    if name in have:
        return False
    work.setdefault("user", {})["collections"] = [*have, name]    # cleaned, as explicit() reads them
    return True


def take(work: dict, name: str) -> bool:
    """Take a work out of a collection and its parts (its explicit memberships:
    out of 219a also means out of 219a/public-economics). True if it was in."""
    have = explicit(work)
    keep = [n for n in have if not matches(n, name)]
    if keep == have:
        return False
    work["user"]["collections"] = keep
    return True


def change(cfg: Config, work_ids: list[str], name: str, *, add: bool = True) -> list[str]:
    """Put works into a collection (registering it if new), or take them out.

    Returns the ids of the works that changed. Only those files are written.
    """
    from . import works

    name = clean_name(name)
    all_works = works.load_all(cfg)
    changed = []
    for work_id in work_ids:
        work = all_works.get(work_id)
        if work is None:
            continue
        if put(work, name) if add else take(work, name):
            records.write_json(works.works_dir(cfg) / f"{work_id}.json", work)
            changed.append(work_id)
    if add:
        registry = load(cfg)
        fresh = name not in registry
        if fresh:
            register(cfg, name, registry=registry)
        if changed:
            # When papers last went in: the sidebar's "recently added to".
            registry[name]["touched"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        if fresh or changed:
            save(cfg, registry)
    return changed


def rename(cfg: Config, old: str, new: str) -> int:
    """Rename a collection (and its sub-collections) everywhere. Returns works changed.

    Only explicit memberships follow; a folder-derived collection is renamed by
    renaming its folder.
    """
    from . import works

    old, new = clean_name(old), clean_name(new)
    renamed = lambda n: new + n[len(old):] if matches(n, old) else n   # noqa: E731
    count = 0
    for work_id, work in works.load_all(cfg).items():
        have = explicit(work)
        after = list(dict.fromkeys(renamed(n) for n in have))
        if after != have:
            work["user"]["collections"] = after
            records.write_json(works.works_dir(cfg) / f"{work_id}.json", work)
            count += 1
    registry = load(cfg)
    moved = {renamed(n): entry for n, entry in registry.items() if matches(n, old)}
    if moved:
        registry = {n: e for n, e in registry.items() if not matches(n, old)} | moved
        save(cfg, registry)
    return count


# --- The sidebar: order and hidden collections --------------------------------------------
# Both are details on registry entries, so they sync with the library, follow
# a rename and go with a delete. `order` is a collection's place among its
# siblings: projects in the sidebar, as a part's `order` is its place in the
# course's reading list. A folder-derived collection is registered when first
# placed or hidden, with the kind and term its folders give it.

def _ensure(cfg: Config, registry: dict[str, dict], name: str, derived: dict[str, dict]) -> dict:
    if name not in registry:
        found = derived.get(name) or {}
        register(cfg, name, kind=found.get("kind"), term=found.get("term") or "", registry=registry)
    return registry[name]


# Which detail holds a place: `order` among the projects, `pinned` among the pins.
PLACES = ("order", "pinned")


def arrange(cfg: Config, names: list[str], conn: sqlite3.Connection | None = None,
            field: str = "order") -> list[str]:
    """Put collections in this order (1, 2, …) among the projects, or with
    field="pinned" among the pinned ones. Returns the cleaned names."""
    if field not in PLACES:
        raise Invalid(f"no such place: {field}")
    registry, derived = load(cfg), derived_details(conn) if conn is not None else {}
    names = list(dict.fromkeys(clean_name(n) for n in names))
    for at, name in enumerate(names, start=1):
        _ensure(cfg, registry, name, derived)[field] = at
    save(cfg, registry)
    return names


def unarrange(cfg: Config, names: list[str]) -> None:
    """Forget the order of these collections (they go back to A–Z)."""
    registry = load(cfg)
    for name in (clean_name(n) for n in names):
        registry.get(name, {}).pop("order", None)
    save(cfg, registry)


def pin(cfg: Config, names: list[str], pinned: bool = True,
        conn: sqlite3.Connection | None = None) -> list[str]:
    """Pin collections (any course, project or part) at the top of the
    sidebar, after the ones pinned before; or unpin them."""
    registry, derived = load(cfg), derived_details(conn) if conn is not None else {}
    names = list(dict.fromkeys(clean_name(n) for n in names))
    last = max((e.get("pinned") or 0 for e in registry.values()), default=0)
    for name in names:
        if pinned and not (registry.get(name) or {}).get("pinned"):
            last += 1
            _ensure(cfg, registry, name, derived)["pinned"] = last
        elif not pinned and name in registry:
            registry[name].pop("pinned", None)
    save(cfg, registry)
    return names


def set_hidden(cfg: Config, names: list[str], hidden: bool = True,
               conn: sqlite3.Connection | None = None) -> list[str]:
    """Hide collections from the sidebar, or show them again. Their papers,
    memberships and search (#name) are untouched. Returns the cleaned names."""
    registry, derived = load(cfg), derived_details(conn) if conn is not None else {}
    names = list(dict.fromkeys(clean_name(n) for n in names))
    for name in names:
        if hidden:
            _ensure(cfg, registry, name, derived)["hidden"] = True
        elif name in registry:
            registry[name].pop("hidden", None)
    save(cfg, registry)
    return names


def delete(cfg: Config, name: str) -> int:
    """Forget a collection and its parts: the registry entries and every paper's
    explicit membership. Returns the number of papers taken out. Papers in it
    because of their folder are untouched (and keep the name alive)."""
    from . import works

    name = clean_name(name)
    count = 0
    for work_id, work in works.load_all(cfg).items():
        if take(work, name):
            records.write_json(works.works_dir(cfg) / f"{work_id}.json", work)
            count += 1
    registry = load(cfg)
    kept = {n: e for n, e in registry.items() if not matches(n, name)}
    if kept != registry:
        save(cfg, kept)
    return count


# --- Listing -------------------------------------------------------------------------------

def listing(cfg: Config, conn: sqlite3.Connection, all_works: dict | None = None) -> list[dict]:
    """Every collection with at least one paper, or registered: name, kind, term,
    folder, shared, bib_file, `works` (papers in it, sub-collections included),
    `own` (papers in it directly), `implicit` (true if it exists only
    because of folders, so it cannot be renamed or emptied from refs),
    `from_folders` (some papers are in it by their folder), `hidden` (left
    out of the sidebar), `pinned` (its place among the pins) and two dates
    for sorting, `created_at` and `used_at`.
    """
    import json

    from . import works

    all_works = works.load_all(cfg) if all_works is None else all_works
    registry = load(cfg)
    # Members by collection, from the index (explicit and folder-derived alike).
    members: dict[str, set[str]] = {}
    for row in conn.execute("SELECT work_id, collections FROM files "
                            "WHERE work_id IS NOT NULL AND collections IS NOT NULL"):
        for name in cleaned(json.loads(row["collections"])):     # an index made before names were cleaned
            members.setdefault(name, set()).add(row["work_id"])
    # Works the index has not seen yet (just added) still count by their own field.
    for work_id, work in all_works.items():
        for name in explicit(work):
            members.setdefault(name, set()).add(work_id)
    folder_born = derived_details(conn)
    # When each paper entered refs, for the dates below.
    entered = {wid: t for wid, w in all_works.items() if (t := _moment(w.get("created"))) is not None}

    names = set(members) | set(registry)
    # A sub-collection's parent is listed even when it has no paper of its own.
    for name in list(names):
        while (name := parent(name)) is not None:
            names.add(name)
    out = []
    for name in sorted(names):
        entry = registry.get(name) or {}
        derived = folder_born.get(name, {})
        inside = set().union(*(ids for n, ids in members.items() if matches(n, name)))
        times = [entered[w] for w in inside if w in entered]
        out.append({
            "name": name,
            "kind": entry.get("kind") or derived.get("kind") or guess_kind(name, registry),
            "term": entry.get("term") or derived.get("term"),
            "folder": entry.get("folder"),
            "shared": bool(entry.get("shared")),
            "bib_file": entry.get("bib_file"),
            "order": entry.get("order"),
            "label": entry.get("label"),
            "registered": name in registry,
            "implicit": name not in registry and bool(derived)
                        and not any(name in explicit(w) for w in all_works.values()),
            # Some papers are in it because of their folder (a rename leaves those).
            "from_folders": bool(derived),
            "hidden": bool(entry.get("hidden")),
            "pinned": entry.get("pinned"),
            # Seconds since 1970, for the sidebar's sorts: when it was made
            # (else when its first paper entered refs), and when papers last
            # went in (else when its newest paper entered refs).
            "created_at": _moment(entry.get("created")) or (min(times) if times else None),
            "used_at": max(filter(None, [_moment(entry.get("touched")), max(times, default=None)]), default=None),
            "works": len(inside),
            "own": len(members.get(name, set())),
        })
    return out


def derived_details(conn: sqlite3.Connection) -> dict[str, dict]:
    """Kind and term of each folder-derived collection, from the index's paths.
    A course code seen in several terms gets the latest one."""
    found: dict[str, dict] = {}
    rows = conn.execute("SELECT DISTINCT context, area, term, slug, section, topic, topic_order "
                        "FROM paths WHERE context IS NOT NULL").fetchall()
    for row in rows:
        ctx = contexts.Context(row["context"], row["area"] or "", row["term"], row["slug"])
        if (got := implicit(ctx)) is None:
            continue
        name, kind, term = got
        before = found.get(name)
        if before is None or (term and (before["term"] is None or _term_rank(term) > _term_rank(before["term"]))):
            found[name] = {"kind": kind, "term": term}
    return found


def _moment(stamp: str | None) -> float | None:
    """An ISO time stamp (refs', or Zotero's "2023-04-05 12:34:56") as
    seconds; a stamp without a zone is UTC, as both store it."""
    if not stamp:
        return None
    try:
        when = datetime.fromisoformat(stamp)
    except (TypeError, ValueError):
        return None
    return (when if when.tzinfo else when.replace(tzinfo=timezone.utc)).timestamp()


_SEASON_RANK = {"winter": 0, "spring": 1, "summer": 2, "fall": 3}


def _term_rank(term: str) -> int:
    year, _, season = term.partition("-")
    return int(year) * 4 + _SEASON_RANK.get(season, 0) if year.isdigit() else -1
