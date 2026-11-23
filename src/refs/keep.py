"""Which copy to keep: a recommendation for each set under Review → copies.

A set is one paper stored more than once: the same file twice, a copy you
marked up next to a clean one, a working paper next to the published article.
For each set refs says which copy to keep and what to do with each other one,
and why. The reasons, in order of weight:

1. **Your marks.** The copy you highlighted and wrote in is the one you read:
   it is kept. A copy whose marks (author by author) are all in the kept copy
   loses nothing when it goes; one with marks the kept copy lacks is never
   proposed for the Trash: its marks can be brought over first (annotations.pull).
2. **The version.** Files of one *edition* (same version, about the same
   length) are interchangeable apart from their marks. Between editions the
   published article is preferred to the working paper, and a newer draft to an
   older one, but these are only suggestions (a working paper may carry the
   appendix): "trash?" rather than "trash". A file of a few pages next to a full
   one is likely a preview. Two published files of quite different length are
   both kept.
3. **Place and name.** The library folder's copy, under the tidy name, without
   a "-2" or "(1)", beats one elsewhere.

Each other copy gets one of: "trash" (nothing is lost: the same file, or no
marks the kept copy lacks), "trash?" (another version you may still want),
"merge" (has marks the kept copy lacks), "keep" (keep it too). Nothing here
changes a file; `apply` does what a set's recommendation says, when asked.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import PurePosixPath

from . import index, library, organize, records, rename, storage
from .config import Config

TRASH, MAYBE, MERGE, KEEP = "trash", "trash?", "merge", "keep"
PREVIEW_PAGES = 10            # a file this short next to a long one may be a preview
_SUFFIX = re.compile(r"(-\d+|\s\(\d+\)|\scopy(\s\d+)?)$", re.IGNORECASE)


@dataclass
class Copy:
    path: str
    hash: str
    pages: int
    your_pages: int
    your_marks: int
    by_author: dict
    latest: str | None
    size: int
    mtime: float
    library: bool
    managed: str | None        # why refs may not touch it (canvas-sync, Git), if so
    version: str               # "published", "working paper" or "unknown"
    tidy: bool                 # has the name refs would give it
    suffixed: bool             # "-2", " (1)", " copy"
    made: str | None           # the PDF's own creation date (drafts: which is newer)
    action: str = ""
    why: str = ""

    @property
    def marks(self) -> int:
        return sum(self.by_author.values())


def _version(read: dict | None) -> str:
    """"published", "working paper" or "unknown", from a file's read record."""
    if not read:
        return "unknown"
    reading = read.get("reading") or {}
    if reading.get("is_working_paper"):
        return "working paper"
    record = (read.get("registry") or {}).get("record") or {}
    if record.get("type") in {"posted-content", "report"}:
        return "working paper"
    return "published" if record or reading else "unknown"


def _made(cfg: Config, rel: str) -> str | None:
    """The PDF's own creation date; None for an online-only file (opening it
    would download it) or one without a date."""
    import os

    import pymupdf

    from .reading import pdf_date
    path = cfg.absolute(rel)
    try:
        if getattr(os.stat(path), "st_flags", 0) & storage.SF_DATALESS:
            return None
        doc = pymupdf.open(path)
    except Exception:                                   # noqa: BLE001 (gone, damaged)
        return None
    try:
        return pdf_date(doc.metadata.get("creationDate")) if doc.metadata else None
    finally:
        doc.close()


def facts(cfg: Config, conn: sqlite3.Connection, work: dict | None, paths: list[str], *,
          supersedes: dict[str, str | None] | None = None) -> list[Copy]:
    """What refs knows about each copy of a paper."""
    if supersedes is None:
        supersedes = dict(conn.execute("SELECT hash, supersedes FROM files").fetchall())
    found = []
    for rel in paths:
        row = conn.execute(
            "SELECT f.hash, f.pages, f.marked_pages, f.marks, f.marks_by, f.marks_latest, p.size, p.mtime "
            "FROM paths p JOIN files f ON f.hash = p.hash WHERE p.path=?", (rel,)).fetchone()
        if row is None:
            continue
        # An annotated file is a new version of the one it replaced: its read
        # record is the earlier version's.
        read = next((r for h in index._ancestors(row["hash"], supersedes)
                     if (r := records.load_read(cfg, h)) is not None), None)
        name = PurePosixPath(rel).name
        wanted = rename.proposed_name(work, name) if work else None
        found.append(Copy(
            path=rel, hash=row["hash"], pages=row["pages"] or 0, your_pages=row["marked_pages"] or 0,
            your_marks=row["marks"] or 0, by_author=json.loads(row["marks_by"] or "{}"),
            latest=row["marks_latest"], size=row["size"] or 0, mtime=row["mtime"] or 0,
            library=library.holds(cfg, rel), managed=rename.blocked_reason(cfg, rel),
            version=_version(read), tidy=bool(wanted) and wanted == name,
            suffixed=bool(_SUFFIX.search(PurePosixPath(name).stem)), made=None))
    return found


def _contained(mine: dict, theirs: dict) -> bool:
    """Are all of `mine`'s marks (author by author, by count) in `theirs`?"""
    return all(n <= theirs.get(author, 0) for author, n in mine.items())


def _same_edition(a: Copy, b: Copy) -> bool:
    """Same version and about the same length: copies of one edition."""
    if a.hash == b.hash:
        return True
    versions = {a.version, b.version} - {"unknown"}
    if len(versions) > 1:
        return False
    longest = max(a.pages, b.pages, 1)
    return abs(a.pages - b.pages) <= max(1, round(0.05 * longest))


def _editions(copies: list[Copy]) -> list[list[Copy]]:
    groups: list[list[Copy]] = []
    for copy in copies:
        home = next((g for g in groups if any(_same_edition(copy, other) for other in g)), None)
        if home is None:
            groups.append([copy])
        else:
            home.append(copy)
    return groups


def _other_draft(copy: Copy, best: Copy) -> str | None:
    """Why two files of a working paper may be different drafts, or None.
    Published files differ by where they were downloaded, not by draft."""
    if copy.version != "working paper" or best.version != "working paper":
        return None
    if copy.made and best.made:
        if copy.made[:10] == best.made[:10]:
            return None
        older = "an earlier" if copy.made < best.made else "a later"
        return f"{older} draft ({copy.made[:10]}; the kept one is {best.made[:10]})"
    return "another file of the working paper; refs cannot tell if it is the same draft"


def _best(group: list[Copy]) -> Copy:
    """The copy of an edition to keep: your marks, then everyone's, then the
    library copy, a tidy name, no "-2", one refs may touch, then the newest."""
    return max(group, key=lambda c: (c.your_pages, c.marks, c.library, c.tidy, not c.suffixed,
                                     c.managed is None, c.mtime))


def _describe(copy: Copy) -> str:
    parts = [copy.version if copy.version != "unknown" else "", f"{copy.pages} pages" if copy.pages else ""]
    if copy.your_pages:
        parts.append(f"your marks on {copy.your_pages} page{'s' if copy.your_pages != 1 else ''}")
    return ", ".join(p for p in parts if p)


def recommend(cfg: Config, conn: sqlite3.Connection, work: dict | None, paths: list[str], *,
              supersedes: dict[str, str | None] | None = None) -> dict:
    """The recommendation for one paper's copies: {"keep", "verdict", "summary",
    "copies": [...each copy's facts, action and why], "rename_to"}. The
    verdict: "safe" (only safe trashing), "optional" (some versions you may
    want), "merge" (marks to bring over first), "keep" (keep them all)."""
    copies = facts(cfg, conn, work, paths, supersedes=supersedes)
    if len(copies) < 2:
        return {"keep": copies[0].path if copies else None, "verdict": "keep", "summary": "", "copies": [],
                "rename_to": None}
    # Drafts are told apart by the dates the PDFs carry (read only for working papers).
    for copy in copies:
        if copy.version == "working paper":
            copy.made = _made(cfg, copy.path)
    editions = _editions(copies)
    bests = [_best(group) for group in editions]

    # The edition to keep: the one with your marks; else the published one
    # (not a short preview of it); else the newest draft.
    longest = max(c.pages for c in copies)

    def preview(c: Copy) -> bool:
        return 0 < c.pages < PREVIEW_PAGES and c.pages * 3 < longest

    order = {"published": 2, "unknown": 1, "working paper": 0}
    primary = max(bests, key=lambda b: (b.your_pages, b.marks, not preview(b), order[b.version],
                                        b.made or "", b.pages))

    for group, best in zip(editions, bests):
        # Within an edition: keep its best copy; the others go unless they
        # hold marks it lacks.
        for copy in group:
            if copy is best:
                continue
            if copy.managed:
                copy.action, copy.why = KEEP, f"refs leaves it alone ({copy.managed})"
            elif copy.hash == best.hash:
                copy.action, copy.why = TRASH, "the same file"
            elif _contained(copy.by_author, best.by_author) and _other_draft(copy, best):
                copy.action, copy.why = MAYBE, _other_draft(copy, best)
            elif _contained(copy.by_author, best.by_author):
                copy.action = TRASH
                copy.why = "its marks are all in the kept copy" if copy.marks else \
                    ("no marks; the kept copy has yours" if best.your_pages else "another copy, no marks")
            elif copy.pages == best.pages:
                copy.action, copy.why = MERGE, "has marks the kept copy lacks: bring them over first"
            else:
                copy.action, copy.why = KEEP, "has marks of its own"
        if best is primary:
            best.action, best.why = KEEP, "kept: " + (_describe(best) or "the best copy")
            continue
        # Another edition: a suggestion at most.
        if best.your_pages or best.marks:
            best.action, best.why = KEEP, "your marks are on this version too" if best.your_pages \
                else "has marks of its own"
        elif best.managed:
            best.action, best.why = KEEP, f"refs leaves it alone ({best.managed})"
        elif preview(best):
            best.action, best.why = MAYBE, f"only {best.pages} pages: a preview?"
        elif best.version == "working paper" and primary.version != "working paper":
            extra = f" (it has {best.pages - primary.pages} more pages: an appendix?)" \
                if best.pages > primary.pages * 1.2 else ""
            best.action, best.why = MAYBE, f"the working paper; the published version is kept{extra}"
        elif best.version == "working paper":
            best.action, best.why = MAYBE, "an earlier draft" if (best.made or "") < (primary.made or "") \
                else "another draft"
        elif primary.your_pages:
            best.action, best.why = MAYBE, "another version; you read the kept one"
        else:
            best.action, best.why = KEEP, "another version of different length: keep both?"

    actions = {c.action for c in copies if c is not primary}
    verdict = ("merge" if MERGE in actions else "optional" if MAYBE in actions
               else "safe" if actions == {TRASH} else "keep")
    going = sum(c.action == TRASH for c in copies)
    maybe = sum(c.action == MAYBE for c in copies)
    summary = {
        "safe": f"Keep {_who(primary)}; {going} {'copy' if going == 1 else 'copies'} can go, nothing is lost.",
        "optional": f"Keep {_who(primary)}"
                    + (f"; {going} {'copy' if going == 1 else 'copies'} can go" if going else "")
                    + f"; {maybe} other version{'s' if maybe != 1 else ''} you may not need.",
        "merge": f"Keep {_who(primary)}; another copy has marks it lacks: bring them over first.",
        "keep": "Keep them all: " + ("they are different versions." if len(editions) > 1 else "each has its own marks."),
    }[verdict]

    # Once the others are gone, the kept copy can take the tidy name.
    rename_to = None
    if work and not primary.tidy and not primary.managed:
        wanted = rename.proposed_name(work, PurePosixPath(primary.path).name)
        folder = cfg.absolute(primary.path).parent
        leaving = {PurePosixPath(c.path).name for c in copies
                   if c.action == TRASH and str(PurePosixPath(c.path).parent) == str(PurePosixPath(primary.path).parent)}
        if wanted and (not (folder / wanted).exists() or wanted in leaving):
            rename_to = wanted
    me = {name.strip().lower() for name in cfg.annotation_authors}
    return {"keep": primary.path, "verdict": verdict, "summary": summary, "rename_to": rename_to,
            "copies": [asdict(c) | {"marks": c.marks,
                                    "others": {a: n for a, n in c.by_author.items() if a.strip().lower() not in me}}
                       for c in sorted(copies, key=lambda c: c is not primary)]}


def _who(copy: Copy) -> str:
    if copy.your_pages:
        return f"the copy with your marks ({copy.your_pages} page{'s' if copy.your_pages != 1 else ''})"
    if copy.version == "published":
        return "the published version"
    if copy.library and copy.tidy:
        return "the library copy"
    return f"{PurePosixPath(copy.path).name}"


def apply(cfg: Config, conn: sqlite3.Connection, work: dict, paths: list[str], *,
          optional: bool = False) -> dict:
    """Do what the recommendation says for one paper: trash the copies marked
    "trash" (and "trash?" when `optional`), then give the kept copy its tidy
    name if that is free. Recomputed here: what the page showed is not trusted.
    Returns {"trashed": [paths], "renamed": (old, new) or None, "refused": [(path, why)]}."""
    advice = recommend(cfg, conn, work, paths)
    wanted = {TRASH, MAYBE} if optional else {TRASH}
    trashed, refused = [], []
    for copy in advice["copies"]:
        if copy["action"] not in wanted:
            continue
        try:
            organize.trash(cfg, conn, copy["path"], other_version_ok=True)
            trashed.append(copy["path"])
        except (organize.Refused, OSError) as error:
            refused.append((copy["path"], str(error)))
    renamed = None
    if advice["rename_to"] and not any(p == advice["keep"] for p, _ in refused):
        old = advice["keep"]
        keep_hash = next(c["hash"] for c in advice["copies"] if c["path"] == old)
        new = rename.apply(cfg, old, advice["rename_to"], keep_hash)
        if new:
            renamed = (old, new)
            former = work["user"].setdefault("former_names", [])
            if PurePosixPath(old).name not in former:
                former.append(PurePosixPath(old).name)
            from . import works
            records.write_json(works.works_dir(cfg) / f"{work['id']}.json", work)
    return {"trashed": trashed, "renamed": renamed, "refused": refused}
