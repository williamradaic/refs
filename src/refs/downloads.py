"""PDFs in your Downloads folder, outside the library, and bringing them in.

A literature review often ends with a pile of PDFs in ~/Downloads. The page's
Downloads view lists them, and takes the ones you tick into the quick-add
folder or straight into a course or project folder, after which they are read
like any other new PDF (`refs add`, run by the web server).

Downloads also holds receipts, tickets, bank statements and forms, so each PDF
is first sorted, on this Mac and for free (no model, nothing sent anywhere),
into "paper", "maybe" and "other", from what is printed in it:

* for a paper: an identifier (DOI, arXiv, NBER, SSRN, JSTOR), a file name
  that looks like one ("w31234.pdf", "Berry 1995 ..."), an abstract, JEL codes
  or keywords, a reference list near the end, citations in the text, the
  words of a journal or working-paper series, and length;
* against: one or two pages, landscape pages (slides), and phrases that only
  paperwork uses ("amount due", "boarding pass", "order number"). Single
  words such as "tax" or "payment" are not used: papers are full of them.

The verdict only decides what the view shows first; any PDF can be taken in.

What this module touches in Downloads is deliberately narrow: files directly
inside the folder (no subfolders), named *.pdf, given by name only. Nothing
there is ever overwritten or deleted; a move takes a file out, a copy leaves it.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import sqlite3
import unicodedata
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import pymupdf

from . import extract, naming, organize, rename
from .config import Config

# How many PDFs the view lists, newest first. Each is read once (about 20 ms)
# and remembered until it changes, but a Downloads folder of years can hold
# thousands; the older ones are counted, not listed.
LIMIT = 300

# A PDF still being written by the browser has no end marker yet (ingest.py).
TAIL_BYTES = 4096


class Refused(organize.Refused):
    """An action refs will not take; the message says why."""


# --- Where --------------------------------------------------------------------------------

def folder(cfg: Config) -> Path | None:
    """The Downloads folder, or None when it is off, missing, or inside the
    library (whose PDFs refs already sees)."""
    if cfg.downloads_folder is None or not cfg.downloads_folder.is_dir():
        return None
    resolved = cfg.downloads_folder.resolve()
    if resolved.is_relative_to(cfg.dropbox_root):
        return None
    return resolved


def _safe_name(name: str) -> bool:
    """A plain PDF file name: no folder part, no hidden file, nothing odd."""
    return (bool(name) and name == PurePosixPath(name).name and name not in {".", ".."}
            and not name.startswith(".") and "\\" not in name and "\0" not in name
            and name.lower().endswith(".pdf"))


def _pdfs(where: Path) -> list[os.DirEntry]:
    """The PDFs directly inside `where`, real files only (no links, no folders)."""
    with os.scandir(where) as entries:
        return [e for e in entries
                if _safe_name(e.name) and e.is_file(follow_symlinks=False)]


# --- Is it a paper? -----------------------------------------------------------------------

# Phrases of paperwork, not of research: each one found counts against.
PAPERWORK = re.compile(
    r"\b(invoice (?:no|number|date|#)|amount due|balance due|total due|subtotal|order (?:no\.?|number|#)"
    r"|boarding pass|confirmation (?:code|number)|booking (?:reference|number)|itinerary"
    r"|account (?:no\.?|number)|statement (?:date|period)|billing address|shipping address"
    r"|policy (?:no\.?|number)|date of birth|social security|passport (?:no\.?|number)"
    r"|tracking number|pay this amount|payment due|thank you for your (?:order|purchase|payment)"
    r"|receipt (?:no\.?|number|#)|sales tax|form w-2|form 1099|form 1040|lease agreement)\b",
    re.IGNORECASE)
# The opening of a paper: its abstract, JEL codes or keywords.
ABSTRACT = re.compile(r"\babstract\b", re.IGNORECASE)
FRONT_MATTER = re.compile(r"\bJEL\b|\bkey ?words\b|\bclassification codes?\b", re.IGNORECASE)
# Where a paper comes from: a journal, a series, a department.
VENUE = re.compile(
    r"\b(journal of|review of|econometrica|proceedings|quarterly|working paper|discussion paper"
    r"|NBER|national bureau of economic|department of economics|university|forthcoming"
    r"|preprint|manuscript|volume \d|vol\. \d)\b", re.IGNORECASE)
# A numbered series: "Working Paper No. 1646", "Discussion Paper 23", "IDB-WP-1646".
SERIES = re.compile(r"\b(?:working|discussion|staff|policy research) paper\s*(?:no\.?|series|#|\d)"
                    r"|\bWP[- ]?\d{2,}", re.IGNORECASE)
# A CV lists papers and cites them, but is not one.
CV_NAME = re.compile(r"(?<![a-z])(?:cv|resume|résumé|curriculum)(?![a-z])", re.IGNORECASE)
CV_TEXT = re.compile(r"curriculum vitae|research interests|citizenship:", re.IGNORECASE)
REFERENCES = re.compile(r"\b(references|bibliography|works cited|literature cited)\b", re.IGNORECASE)
# Author-year citations: "Berry (1994)", "(Berry et al., 1995)", "et al.", and
# reference-list entries in Chicago style: "Weyl, E. Glen. 2012. “Market …".
CITATION = re.compile(r"\bet al\.|\(\d{4}[a-z]?\)|, (?:19|20)\d{2}[a-z]?[;)]|\b(?:19|20)\d{2}[a-z]?\. [“\"]")


@dataclass
class Verdict:
    kind: str           # paper | maybe | other | unreadable | incomplete
    why: str            # a few words: what decided it
    pages: int = 0


def _complete(path: Path) -> bool:
    """Does the file start like a PDF and end with the end marker?"""
    try:
        with path.open("rb") as handle:
            if handle.read(5) != b"%PDF-":
                return False
            handle.seek(0, os.SEEK_END)
            handle.seek(max(0, handle.tell() - TAIL_BYTES))
            return b"%%EOF" in handle.read()
    except OSError:
        return False


def judge(path: Path) -> Verdict:
    """Sort one PDF into paper / maybe / other, from its first and last pages."""
    if not _complete(path):
        return Verdict("incomplete", "not a whole PDF (still downloading?)")
    try:
        with pymupdf.open(path) as doc:
            pages = doc.page_count
            first = extract.clean_text(" ".join(doc[i].get_text() for i in range(min(3, pages))))
            # extract.clean_text keeps 10,000 characters; the end needs more.
            # The reference list can sit before long appendices: read the last eight pages.
            tail = " ".join(doc[i].get_text() for i in range(max(3, pages - 8), pages))
            last = re.sub(r"\s+", " ", unicodedata.normalize("NFKC", tail))[-40000:]
            landscape = pages > 0 and doc[0].rect.width > doc[0].rect.height * 1.1
    except Exception as error:                     # damaged or password-protected
        return Verdict("unreadable", f"could not be opened ({type(error).__name__})")

    # Signs of a paper's content; at least one is needed to be listed at all,
    # so length and a university's name alone (a form, a letter) are not enough.
    score, reasons, content = 0, [], False

    def note(points: int, reason: str, *, sign: bool = False) -> None:
        nonlocal score, content
        score += points
        content = content or sign
        reasons.append(reason)

    guess = naming.parse_filename(path.name)
    if guess.style in {"id", "jstor", "convention"} or guess.looks_like_paper:
        note(2, "paper-like name", sign=True)
    if CV_NAME.search(path.stem):
        note(-4, "a CV?")
    if len(first) < 200:
        # A scan without a text layer: only the name and the length can tell.
        kind = "maybe" if score > 0 or (pages >= 6 and score == 0) else "other"
        return Verdict(kind, ", ".join([*reasons, f"no text, {pages} page{'s' * (pages != 1)}"]), pages)

    ids = extract.find_ids(first[:6000])
    if ids:
        note(3, " ".join(sorted(ids)).replace("doi", "DOI"), sign=True)
    if ABSTRACT.search(first):
        note(2, "abstract", sign=True)
    if FRONT_MATTER.search(first):
        note(1, "JEL/keywords", sign=True)
    if SERIES.search(first):
        note(2, "paper series", sign=True)
    if REFERENCES.search(last) and pages >= 4:
        note(2, "references", sign=True)
    if len(CITATION.findall(first + " " + last)) >= 5:
        note(1, "citations", sign=True)
    if VENUE.search(first):
        note(1, "journal/university")
    if pages >= 8:
        note(1, f"{pages} pages")
    elif pages <= 2:
        note(-2, f"{pages} page{'s' * (pages != 1)}")
    if landscape:
        note(-2, "landscape (slides?)")
    if not CV_NAME.search(path.stem) and CV_TEXT.search(first[:3000]):
        note(-4, "a CV?")
    paperwork = {m.lower() for m in PAPERWORK.findall(first)}
    if paperwork:
        note(-2 * min(3, len(paperwork)), f"“{sorted(paperwork)[0]}”")

    if not content:
        return Verdict("other", ", ".join(reasons) or "nothing paper-like", pages)
    kind = "paper" if score >= 4 else "maybe" if score >= 1 else "other"
    return Verdict(kind, ", ".join(reasons) or "nothing paper-like", pages)


# --- Listing ------------------------------------------------------------------------------

# (path, size, mtime_ns) -> (hash, Verdict): a file is read again only when it changes.
_seen: dict[tuple[str, int, int], tuple[str, Verdict]] = {}


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _look(path: Path, stat: os.stat_result) -> tuple[str, Verdict]:
    key = (str(path), stat.st_size, stat.st_mtime_ns)
    if key not in _seen:
        _seen[key] = (_hash(path), judge(path))
    return _seen[key]


def _known(conn: sqlite3.Connection, file_hash: str) -> str | None:
    """Where the library already has this exact file, if it does."""
    row = conn.execute("SELECT path FROM paths WHERE hash=? ORDER BY path LIMIT 1", (file_hash,)).fetchone()
    return row["path"] if row else None


def listing(cfg: Config, conn: sqlite3.Connection, limit: int = LIMIT) -> dict:
    """The PDFs in Downloads, newest first, each with its verdict and, when the
    library has the same file already, where it is."""
    where = folder(cfg)
    if where is None:
        return {"folder": None, "items": [], "older": 0}
    entries = sorted(_pdfs(where), key=lambda e: e.stat().st_mtime, reverse=True)
    items = []
    for entry in entries[:limit]:
        try:
            stat = entry.stat()
            file_hash, verdict = _look(Path(entry.path), stat)
        except OSError:
            continue                                # removed or unreadable meanwhile
        items.append({"name": entry.name, "size": stat.st_size, "modified": stat.st_mtime,
                      "kind": verdict.kind, "why": verdict.why, "pages": verdict.pages,
                      "in_refs": _known(conn, file_hash)})
    return {"folder": str(where), "items": items, "older": max(0, len(entries) - limit)}


def waiting(cfg: Config, conn: sqlite3.Connection) -> tuple[int, int]:
    """For the sidebar, without reading anything: (PDFs already looked at that
    seem to be papers and are not in refs, PDFs not looked at yet). The web
    server looks at the second kind in the background (`warm`)."""
    where = folder(cfg)
    if where is None:
        return 0, 0
    entries = sorted(_pdfs(where), key=lambda e: e.stat().st_mtime, reverse=True)[:LIMIT]
    count = unseen = 0
    for entry in entries:
        stat = entry.stat()
        found = _seen.get((entry.path, stat.st_size, stat.st_mtime_ns))
        if found is None:
            unseen += 1
        else:
            count += found[1].kind in {"paper", "maybe"} and _known(conn, found[0]) is None
    return count, unseen


def warm(cfg: Config) -> None:
    """Look at every listed PDF not seen yet (hash and verdict), so the view opens at once."""
    where = folder(cfg)
    if where is None:
        return
    for entry in sorted(_pdfs(where), key=lambda e: e.stat().st_mtime, reverse=True)[:LIMIT]:
        try:
            _look(Path(entry.path), entry.stat())
        except OSError:
            continue                                # gone, or not readable: listed later


# --- Taking in ----------------------------------------------------------------------------

def destination(cfg: Config, folder_rel: str | None) -> tuple[str, Path]:
    """Check where PDFs from Downloads may go: the quick-add folder (None), or
    a folder refs scans, the same ones "file to…" accepts. Returns (rel, path)."""
    if folder_rel is None or folder_rel.strip("/") == cfg.dump.strip("/"):
        rel = cfg.dump.strip("/")
        path = cfg.absolute(rel)
        path.mkdir(parents=True, exist_ok=True)      # the dump may not exist yet
        return rel, path
    rel = folder_rel.strip().strip("/")
    if not rel or ".." in PurePosixPath(rel).parts or rel.startswith("."):
        raise Refused("not a folder in the library")
    in_dump = rel.startswith(cfg.dump.strip("/") + "/")
    if not in_dump and not any(rel == root or rel.startswith(root + "/") for root in cfg.roots):
        raise Refused("that folder is outside the folders refs scans")
    if reason := organize._folder_allowed(cfg, rel):
        raise Refused(f"cannot put PDFs in that folder: {reason}")
    path = cfg.absolute(rel)
    if not path.is_dir():
        raise Refused("that folder does not exist")
    return rel, path


def _free_name(target_dir: Path, name: str) -> Path:
    """`name` in `target_dir`, or name-2.pdf, name-3.pdf ... if it is taken."""
    target = target_dir / name
    stem, number = Path(name).stem, 2
    while target.exists():
        target = target_dir / f"{stem}-{number}.pdf"
        number += 1
    return target


def _source(where: Path, name: str) -> Path | str:
    """The file `name` directly in Downloads, or why it cannot be used."""
    if not _safe_name(name):
        return "not a PDF directly in Downloads"
    source = where / name
    # The name was checked; this also catches a link pointing elsewhere.
    if source.is_symlink() or not source.is_file() or source.resolve().parent != where:
        return "no longer in Downloads"
    return source


def take(cfg: Config, conn: sqlite3.Connection, names: list[str], folder_rel: str | None,
         mode: str = "move") -> dict:
    """Move (or copy) the named PDFs from Downloads into the library.

    Each file is tried on its own. A file the library already has, byte for
    byte, is left in Downloads and reported under `known`; so is the second
    of two identical files in the same batch. Returns {"folder", "taken":
    [(name, new path)], "known": [(name, library path)], "refused": [(name, why)]}.
    The caller holds the ingest lock and starts the reading.
    """
    if mode not in {"move", "copy"}:
        raise Refused("mode is move or copy")
    where = folder(cfg)
    if where is None:
        raise Refused("no Downloads folder is set (settings)")
    rel_dir, target_dir = destination(cfg, folder_rel)
    taken, known, refused = [], [], []
    batch: dict[str, str] = {}                      # hash -> name, within this batch
    for name in dict.fromkeys(names):               # each name once, in order
        source = _source(where, name)
        if isinstance(source, str):
            refused.append((name, source))
            continue
        if not _complete(source):
            refused.append((name, "not a whole PDF (still downloading?)"))
            continue
        try:
            file_hash = _hash(source)
            if (path := _known(conn, file_hash)) is not None:
                known.append((name, path))
                continue
            if file_hash in batch:
                known.append((name, f"the same file as {batch[file_hash]}"))
                continue
            target = _free_name(target_dir, name)
            if mode == "copy":
                shutil.copy2(source, target)        # keeps the modification time
            else:
                shutil.move(source, target)
        except OSError as error:
            refused.append((name, f"{type(error).__name__}: {error}"))
            continue
        batch[file_hash] = name
        taken.append((name, f"{rel_dir}/{target.name}"))
    return {"folder": rel_dir, "taken": taken, "known": known, "refused": refused}


# --- Clearing out what refs has -----------------------------------------------------------

def trash_known(cfg: Config, conn: sqlite3.Connection, names: list[str]) -> dict:
    """Move to the macOS Trash the named PDFs in Downloads that the library
    already has, byte for byte (typically after "copy" took them in).

    Each file is hashed again here, so only a file whose exact content is in
    the index can go: anything else (a different version, a file never taken
    in) is refused and stays. The Trash keeps the name, so a file can be
    dragged back. Returns {"trashed": [(name, library path)], "refused":
    [(name, why)]}.
    """
    where = folder(cfg)
    if where is None:
        raise Refused("no Downloads folder is set (settings)")
    trashed, refused = [], []
    for name in dict.fromkeys(names):
        source = _source(where, name)
        if isinstance(source, str):
            refused.append((name, source))
            continue
        try:
            path = _known(conn, _hash(source))
            if path is None:
                refused.append((name, "refs does not have this file; it stays"))
                continue
            rename.to_trash(source)
        except OSError as error:
            refused.append((name, f"{type(error).__name__}: {error}"))
            continue
        trashed.append((name, path))
    return {"trashed": trashed, "refused": refused}
