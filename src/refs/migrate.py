"""Moving every reading into the library folder (phase 3 of the research-first plan).

Before: readings live in course and project folders, and a paper's course is
read off its folder. After: every reading is in the library folder once,
under its canonical name, and its courses and projects are collections. Course
material (slides, problem sets, syllabi) stays where it is and leaves the
index once the watched folders are switched.

Three steps, each one reviewed before it runs:

1. `plan`: for every paper (a work) with no PDF in the library folder, its
   best PDF elsewhere goes in (moved; copied out of shared folders, so
   coauthors see no change, and out of folders other tools manage). Every
   paper also keeps the courses and projects its folders gave it, as
   collections of its own, with each readings/<topic> folder as a part
   ("220a/demand-estimation-ii", in the course's order).
2. `apply`: does the ticked rows, journaled, and keeps a record of the batch
   in <state>/migrations/ so `undo` can put everything back (files and
   memberships). Optionally writes readings.md into each course folder.
3. `finish`: renames the library folder (papers-dump -> refs-library), and
   switches the watched folders to the library alone (plus shared folders),
   with the course folders as inboxes (inbox.py). Settings are saved through
   settings.py, comments kept, the previous file as config.toml.bak.
"""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from . import collection, contexts, library, records, rename, shared, works
from .config import Config


@dataclass
class Row:
    """One paper in the plan."""

    work_id: str
    citekey: str
    title: str
    source: str | None          # the PDF that goes into the library; None = already there
    mode: str | None            # "move" or "copy" (None when nothing moves)
    why: str                    # why that mode, or why nothing moves
    collections: list[str]      # memberships to make explicit (parts included)
    group: str                  # where it comes from, for the review: a context key
    target: str | None = None   # filled by apply


@dataclass
class Plan:
    rows: list[Row] = field(default_factory=list)
    parts: dict[str, dict] = field(default_factory=dict)   # part -> {"order", "label"}
    unidentified: list[str] = field(default_factory=list)  # paper-like PDFs never read
    inboxes: list[str] = field(default_factory=list)       # course areas, for `finish`

    def to_dict(self) -> dict:
        return {"rows": [asdict(r) for r in self.rows], "parts": self.parts,
                "unidentified": self.unidentified, "inboxes": self.inboxes}


def _area_folder(ctx: contexts.Context) -> str | None:
    """The folder that holds a course context's area: "phd/coursework" for
    phd/coursework/2026-fall/220a."""
    if not ctx.folder:
        return None
    parts = ctx.folder.split("/")
    lowered = [p.lower() for p in parts]
    if ctx.area.lower() in lowered:
        return "/".join(parts[:lowered.index(ctx.area.lower()) + 1])
    return None


def plan(cfg: Config, conn: sqlite3.Connection) -> Plan:
    """What the migration would do. Reads only."""
    layout = contexts.layout_for(cfg)
    shared_map = shared.folders(cfg)
    all_works = works.load_all(cfg)
    result = Plan()
    inbox_areas: set[str] = set()

    # Every location of every work's files, with its context and your marks.
    located: dict[str, list[sqlite3.Row]] = {}
    for row in conn.execute("SELECT p.path, f.work_id, f.marked_pages, p.mtime FROM paths p "
                            "JOIN files f ON f.hash = p.hash WHERE f.work_id IS NOT NULL ORDER BY p.path"):
        located.setdefault(row["work_id"], []).append(row)

    for work_id, rows in located.items():
        work = all_works.get(work_id)
        if work is None or "meta" not in work:
            continue
        names: dict[str, None] = {}
        for row in rows:
            ctx = layout.derive(row["path"])
            if (part := collection.suggested(ctx)) is not None:
                names[part] = None
                if "/" in part and part not in result.parts:
                    label, order = contexts.pretty_topic(ctx.topic_folder or "")
                    result.parts[part] = {"order": order, "label": label}
            found = collection.implicit(ctx)
            if found and found[1] == "course" and (area := _area_folder(ctx)):
                inbox_areas.add(area)
        # Memberships it has already need not be made again.
        new = [n for n in names if n not in collection.explicit(work)]
        paths = [r["path"] for r in rows]
        meta = work["meta"]
        title = meta.get("title") or PurePosixPath(paths[0]).name
        first = layout.derive(paths[0]).key
        if library.copy_of(cfg, paths):
            if new:
                result.rows.append(Row(work_id, work["citekey"], title, None, None,
                                       "already in the library", new, "already in the library"))
            continue
        # The PDF to bring in: the one with most of your marks, then the newest.
        best = max(rows, key=lambda r: (r["marked_pages"] or 0, r["mtime"] or 0))
        source = best["path"]
        in_shared = shared.collection_at(cfg, source, shared_map)
        blocked = rename.blocked_reason(cfg, source)
        mode, why = (("copy", f"from #{in_shared}, shared with coauthors") if in_shared
                     else ("copy", blocked) if blocked else ("move", "moved"))
        result.rows.append(Row(work_id, work["citekey"], title, source, mode, why, new,
                               layout.derive(source).key or first))

    # PDFs that look like papers but were never read: nothing to name them by.
    for row in conn.execute("SELECT p.path FROM paths p JOIN files f ON f.hash = p.hash "
                            "WHERE f.status IS NULL AND f.kind IN ('paper', 'book', 'chapter') "
                            "ORDER BY p.path"):
        if not library.holds(cfg, row["path"]):
            result.unidentified.append(row["path"])
    result.rows.sort(key=lambda r: (r.source is None, r.group, r.title.lower()))
    result.inboxes = sorted(inbox_areas)
    return result


# --- Applying and undoing -------------------------------------------------------------------

def _record_dir(cfg: Config) -> Path:
    return cfg.absolute(cfg.state_dir) / "migrations"


def apply(cfg: Config, conn: sqlite3.Connection, the_plan: Plan, work_ids: set[str] | None = None,
          *, readings_md: bool = False) -> dict:
    """Carry out the plan's rows (all, or those of `work_ids`). Returns
    {"done": [rows], "refused": [(work id, why)], "record": file name}."""
    all_works = works.load_all(cfg)
    registry = collection.load(cfg)
    done, refused, taken = [], [], set()
    for row in the_plan.rows:
        if work_ids is not None and row.work_id not in work_ids:
            continue
        work = all_works.get(row.work_id)
        if work is None:
            refused.append((row.work_id, "the paper is gone"))
            continue
        if row.source is not None:
            try:
                row.target = library.place(cfg, conn, row.source, work, mode=row.mode, taken=taken)
            except (library.Refused, OSError) as error:
                refused.append((row.work_id, f"{row.source}: {error}"))
                continue
            taken.add(PurePosixPath(row.target).name)
        added = [n for n in row.collections if collection.put(work, n)]
        if added:
            records.write_json(works.works_dir(cfg) / f"{row.work_id}.json", work)
        for name in added:
            details = the_plan.parts.get(name) or {}
            if name not in registry:
                collection.register(cfg, name, order=details.get("order"), label=details.get("label"),
                                    registry=registry)
        row.collections = added                     # what undo takes back
        done.append(row)
    collection.save(cfg, registry)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    record = _record_dir(cfg) / f"{stamp}.json"
    records.write_json(record, {"at": stamp, "rows": [asdict(r) for r in done], "undone": False})
    if readings_md:
        write_readings_md(cfg, conn)
    return {"done": done, "refused": refused, "record": record.name}


def last_record(cfg: Config) -> Path | None:
    """The newest migration batch not undone yet."""
    folder = _record_dir(cfg)
    for path in sorted(folder.glob("*.json"), reverse=True) if folder.is_dir() else []:
        if not (records.read_json(path) or {}).get("undone"):
            return path
    return None


def undo(cfg: Config) -> dict:
    """Reverse the last migration batch: moved PDFs go back, copies go to the
    Trash (while the original is still there), memberships it made are taken
    away. Returns {"files": n, "papers": n} (what was put back)."""
    path = last_record(cfg)
    if path is None:
        return {"files": 0, "papers": 0}
    batch = records.read_json(path) or {}
    all_works = works.load_all(cfg)
    files = papers = 0
    for row in reversed(batch.get("rows") or []):
        if row.get("target"):
            current, original = cfg.absolute(row["target"]), cfg.absolute(row["source"])
            if row["mode"] == "copy" and current.exists() and original.exists():
                rename.to_trash(current)
                files += 1
            elif row["mode"] == "move" and current.exists() and not original.exists():
                original.parent.mkdir(parents=True, exist_ok=True)
                os.rename(current, original)
                files += 1
        work = all_works.get(row["work_id"])
        if work is not None and row.get("collections"):
            before = collection.explicit(work)
            work["user"]["collections"] = [n for n in before if n not in row["collections"]]
            if work["user"]["collections"] != before:
                records.write_json(works.works_dir(cfg) / f"{row['work_id']}.json", work)
                papers += 1
    batch["undone"] = True
    records.write_json(path, batch)
    return {"files": files, "papers": papers}


# --- readings.md ------------------------------------------------------------------------------

def write_readings_md(cfg: Config, conn: sqlite3.Connection) -> list[str]:
    """Write readings.md into each course folder: the course's readings by
    part, as names and citekeys to look up in the library. Returns the files
    written (Dropbox-relative). A file refs did not write is left alone."""
    layout = contexts.layout_for(cfg)
    registry = collection.load(cfg)
    all_works = works.load_all(cfg)
    # Each course's own folder, from where its PDFs were.
    folders: dict[str, str] = {}
    for (path,) in conn.execute("SELECT path FROM paths"):
        ctx = layout.derive(path)
        found = collection.implicit(ctx)
        if found and found[1] == "course" and ctx.folder:
            folders.setdefault(found[0], ctx.folder)
    written = []
    for course, folder in sorted(folders.items()):
        members = [w for w in all_works.values() if "meta" in w
                   and any(collection.matches(n, course) for n in collection.explicit(w))]
        if not members:
            continue
        groups: dict[str, list[dict]] = {}
        for work in members:
            part = next((n for n in collection.explicit(work) if n.startswith(course + "/")), course)
            groups.setdefault(part, []).append(work)

        def order(name: str) -> tuple:
            entry = registry.get(name) or {}
            return (name != course, entry.get("order") if entry.get("order") is not None else 999, name)
        lines = [f"# {course}: readings", "",
                 f"Written by refs ({datetime.now():%Y-%m-%d}). The PDFs are in the library "
                 f"folder ({library.folder(cfg)}); look a paper up in refs by its citekey "
                 f"(`refs find <citekey>`), or the whole course with `#{course}`.", ""]
        for part in sorted(groups, key=order):
            entry = registry.get(part) or {}
            if part != course:
                number = f"{entry['order']:02d} " if entry.get("order") is not None else ""
                lines += [f"## {number}{entry.get('label') or part.split('/', 1)[1]}", ""]
            for work in sorted(groups[part], key=lambda w: (w["meta"].get("year") or 0, w["citekey"])):
                meta = work["meta"]
                who = ", ".join(a.get("family", "") for a in (meta.get("authors") or [])[:3])
                lines.append(f"- {who} ({meta.get('year') or 'n.d.'}). {meta.get('title') or ''} "
                             f"`{work['citekey']}`")
            lines.append("")
        target = cfg.absolute(folder) / "readings.md"
        if target.exists() and "Written by refs" not in target.read_text(encoding="utf-8", errors="replace"):
            continue                                # someone else's readings.md
        target.write_text("\n".join(lines), encoding="utf-8")
        written.append(f"{folder}/readings.md")
    return written


# --- Finishing: the switch ------------------------------------------------------------------------

def finish_changes(cfg: Config, the_plan: Plan, library_name: str | None) -> dict:
    """The settings `finish` would save: the library folder (renamed), watched
    folders = the library alone, inboxes = the course areas."""
    new_dump = (library_name or cfg.dump).strip("/")
    inboxes = list(dict.fromkeys([*cfg.inboxes, *the_plan.inboxes]))
    return {"dump": new_dump, "roots": [new_dump], "inboxes": inboxes}


def finish(cfg: Config, the_plan: Plan, library_name: str | None = None) -> dict:
    """Rename the library folder if asked, then save the new settings.
    Returns {"renamed": (old, new) or None, "settings": the changes}."""
    from . import settings

    changes = finish_changes(cfg, the_plan, library_name)
    renamed = None
    old, new = cfg.dump.strip("/"), changes["dump"]
    if new != old:
        source, target = cfg.absolute(old), cfg.absolute(new)
        if target.exists():
            raise FileExistsError(f"{new} exists already; pick another name or merge it by hand")
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            os.rename(source, target)              # Dropbox syncs a folder rename as one move
        else:
            target.mkdir()
        renamed = (old, new)
    try:
        settings.save(changes)
    except Exception:
        if renamed:                                # settings refused: put the folder back
            os.rename(cfg.absolute(new), cfg.absolute(old))
        raise
    return {"renamed": renamed, "settings": changes}


def to_json(the_plan: Plan) -> str:
    return json.dumps(the_plan.to_dict(), indent=1, ensure_ascii=False)
