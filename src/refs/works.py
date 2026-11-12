"""Works: one library item per paper, however many PDFs of it exist.

A *file* is one PDF content. A *work* is the paper itself. Two downloads of the
same article, or its working paper and its published version, are several files
of one work. Works are stored as small JSON files in Dropbox (<state>/works/),
one per work.

What persists in a work file and is never regenerated:
  id, citekey, created, user (your read status, tags, notes)
What is rebuilt from the read records on every sync:
  meta, description, tags, files, keys
"""

from __future__ import annotations

import json
import secrets
from datetime import datetime, timezone
from pathlib import Path

from . import naming, records, venues
from .config import Config

_CITEKEY_SKIP = naming.STOPWORDS | {"is", "are", "do", "does", "how", "why", "what", "when", "some"}
_STATUS_RANK = {"verified": 0, "extracted": 1, "needs-review": 2}


def works_dir(cfg: Config) -> Path:
    return cfg.absolute(cfg.state_dir) / "works"


def load_all(cfg: Config) -> dict[str, dict]:
    """Every work, keyed by id."""
    if not works_dir(cfg).is_dir():
        return {}
    loaded = (records.read_json(p) for p in works_dir(cfg).glob("*.json"))
    return {w["id"]: w for w in loaded if w}


# --- Deciding which files are the same work -------------------------------------------

def work_keys(read: dict) -> list[str]:
    """Keys that identify the work a file belongs to. Sharing any key = same work.

    A DOI is the strongest key. The title key (title plus first author's surname)
    is what joins a working paper to its published version, and two scans of an
    article that has no DOI.
    """
    meta = records.final_metadata(read)
    keys = []
    if meta["doi"]:
        keys.append(f"doi:{meta['doi']}")
    working_paper = (read.get("working_paper") or {}).get("record") or {}
    if working_paper.get("doi"):
        keys.append(f"doi:{working_paper['doi']}")
    if meta["title"]:
        first_author = naming.slugify(meta["surnames"][0]) if meta["surnames"] else ""
        keys.append(f"title:{naming.slugify(meta['title'])[:80]}|{first_author}")
    return keys


def make_citekey(surnames: list[str], year: int | None, title: str, taken: set[str]) -> str:
    """`berry1995automobile`: first surname, year, first meaningful title word.

    If that key is already used by another work, a letter is appended (b, c, ...).
    """
    surname = naming.slugify(surnames[0]).replace("-", "") if surnames else "anon"
    words = [w for w in naming.slugify(title or "").split("-") if w and w not in _CITEKEY_SKIP]
    base = f"{surname}{year or 'nd'}{words[0] if words else ''}"
    key, suffix = base, ord("b")
    while key in taken:
        key, suffix = f"{base}{chr(suffix)}", suffix + 1
    return key


# --- Building a work's metadata from its files' read records ---------------------------

def _meta_from(read: dict) -> dict:
    """Full bibliographic metadata of one read record, with provenance."""
    reading = read.get("reading") or {}
    registry = read.get("registry")
    if registry:
        record = registry["record"]
        meta = {k: record.get(k) for k in
                ("title", "authors", "venue", "type", "doi", "volume", "issue", "pages", "publisher")}
        meta["year"] = registry.get("year_printed") or record.get("year")
        meta["source"] = record.get("source")
    else:
        meta = {
            "title": reading.get("title"), "authors": reading.get("authors") or [],
            "year": reading.get("year"), "venue": reading.get("venue"),
            "type": None, "doi": None, "volume": None, "issue": None, "pages": None,
            "publisher": None, "source": "pdf",
        }
    meta["kind"] = reading.get("kind")
    meta["status"] = read["status"]
    meta["is_working_paper"] = bool(reading.get("is_working_paper")) and not registry
    # A repository (RePEc, SSRN, a university archive) is where a file is hosted,
    # not where the work was published: drop it and treat the record as a
    # working paper.
    if venues.is_repository(meta["venue"]):
        meta["is_working_paper"] = meta["kind"] == "paper"
        if meta["type"] in {"journal-article", "article"}:
            meta["type"] = "posted-content"
    meta["venue"] = venues.clean(meta["venue"])
    return meta


EDITABLE = ("title", "authors", "year", "venue", "kind", "type", "doi", "volume", "issue",
            "pages", "publisher")


def apply_user_edits(work: dict) -> None:
    """Lay your own corrections over the pipeline's metadata.

    Fields you edited (work["user"]["overrides"]) always win and are never
    overwritten by re-processing. Editing or confirming a work marks it
    "confirmed", which takes it out of the review queue.
    """
    user = work["user"]
    overrides = {k: v for k, v in (user.get("overrides") or {}).items() if k in EDITABLE}
    if overrides:
        # Giving a paper a venue by hand means it is published there.
        if overrides.get("venue") and "type" not in overrides and work["meta"].get("kind") == "paper":
            overrides["type"] = "journal-article"
        work["meta"].update(overrides)
        work["meta"]["source"] = user.get("override_source") or "you"
    if overrides or user.get("confirmed"):
        work["meta"]["status"] = "confirmed"
        work["meta"]["is_working_paper"] = work["meta"].get("type") in {None, "report", "posted-content"} \
            and work["meta"].get("kind") == "paper" and not venues.abbreviation(work["meta"].get("venue"))


def sync(cfg: Config) -> dict[str, str]:
    """Bring the work files in line with the read records.

    Returns a map from file hash to work id. Safe to run any number of times:
    a file stays in the work it is already in, ids and citekeys never change.
    """
    if not records.reads_dir(cfg).is_dir():
        return {}
    reads = {}
    for path in records.reads_dir(cfg).glob("*.json"):
        read = records.read_json(path)
        if read and (read.get("reading") or {}).get("kind") in records.LIBRARY_KINDS:
            reads[path.stem] = read

    works = load_all(cfg)
    before = {wid: json.dumps(w, sort_keys=True) for wid, w in works.items()}
    by_file = {h: wid for wid, w in works.items() for h in w["files"]}
    by_key = {k: wid for wid, w in works.items() for k in w["keys"]}
    taken = {w["citekey"] for w in works.values()}
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    # 1. Attach every library file to a work: the one it is already in, else one
    #    that shares a key, else a new one. Verified files go first, so a new
    #    work is created from its best record.
    order = sorted(reads, key=lambda h: (_STATUS_RANK.get(reads[h]["status"], 9), h))
    for file_hash in order:
        keys = work_keys(reads[file_hash])
        wid = by_file.get(file_hash) or next((by_key[k] for k in keys if k in by_key), None)
        if wid is None:
            meta = records.final_metadata(reads[file_hash])
            wid = "w" + secrets.token_hex(5)
            works[wid] = {
                "id": wid,
                "citekey": make_citekey(meta["surnames"], meta["year"], meta["title"] or "", taken),
                "created": now, "keys": [], "files": [],
                "user": {"read_status": "unread", "tags": [], "notes": ""},
            }
            taken.add(works[wid]["citekey"])
        work = works[wid]
        if file_hash not in work["files"]:
            work["files"].append(file_hash)
        for key in keys:
            if key not in work["keys"]:
                work["keys"].append(key)
            by_key.setdefault(key, wid)
        by_file[file_hash] = wid

    # 2. Rebuild each work's metadata from its best file: verified beats extracted,
    #    and among equals the published version beats the working paper.
    for work in works.values():
        candidates = [reads[h] for h in work["files"] if h in reads]
        if not candidates:
            continue                      # its files are gone from the reads; keep as is
        best = min(candidates, key=lambda r: (
            _STATUS_RANK.get(r["status"], 9), _meta_from(r)["is_working_paper"]))
        work["meta"] = _meta_from(best)
        apply_user_edits(work)
        reading = best.get("reading") or {}
        work["description"] = reading.get("description")
        work["tags"] = [*reading.get("fields", []), *reading.get("methods", []),
                        *reading.get("topics", [])]

    # 3. Write only the files that changed (keeps Dropbox quiet).
    for wid, work in works.items():
        if before.get(wid) != json.dumps(work, sort_keys=True):
            records.write_json(works_dir(cfg) / f"{wid}.json", work)
    return by_file


def create_stub(cfg: Config, record: dict, source_url: str | None) -> dict:
    """Save a work that has no PDF yet (quick add of a paywalled paper).

    Its keys are stored, so when the PDF later lands in the dump and is
    processed, sync() attaches it here instead of creating a second work.
    """
    surnames = [a["family"] for a in record.get("authors") or [] if a.get("family")]
    keys = []
    if record.get("doi"):
        keys.append(f"doi:{record['doi']}")
    if record.get("title"):
        first = naming.slugify(surnames[0]) if surnames else ""
        keys.append(f"title:{naming.slugify(record['title'])[:80]}|{first}")

    existing = load_all(cfg)
    for work in existing.values():                 # already in the library?
        if set(keys) & set(work["keys"]):
            return work
    work = {
        "id": "w" + secrets.token_hex(5),
        "citekey": make_citekey(surnames, record.get("year"), record.get("title") or "",
                                {w["citekey"] for w in existing.values()}),
        "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "keys": keys, "files": [],
        "meta": {**{k: record.get(k) for k in ("title", "authors", "year", "venue", "type", "doi",
                                                "volume", "issue", "pages", "publisher", "source")},
                 "kind": "paper", "status": "verified", "is_working_paper": False},
        "description": record.get("abstract"), "tags": [],
        "user": {"read_status": "unread", "tags": [], "notes": "", "source_url": source_url},
    }
    records.write_json(works_dir(cfg) / f"{work['id']}.json", work)
    return work


class Importer:
    """Adds references from another program (Zotero) to the works, idempotently.

    A reference joins an existing work when one of its PDFs already belongs to
    that work, or when DOI or title-and-author match; otherwise a new work is
    created with the imported metadata (status "imported").
    """

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.works = load_all(cfg)
        self.by_file = {h: wid for wid, w in self.works.items() for h in w["files"]}
        self.by_key = {k: wid for wid, w in self.works.items() for k in w["keys"]}
        self.taken = {w["citekey"] for w in self.works.values()}
        self.created = self.joined = 0

    def add(self, record: dict, *, file_hashes: list[str], external: dict,
            tags: list[str], collections: list[str], notes: list[str]) -> dict:
        surnames = [a["family"] for a in record.get("authors") or [] if a.get("family")]
        keys = []
        if record.get("doi"):
            keys.append(f"doi:{record['doi']}")
        if record.get("title"):
            first = naming.slugify(surnames[0]) if surnames else ""
            keys.append(f"title:{naming.slugify(record['title'])[:80]}|{first}")

        wid = next((self.by_file[h] for h in file_hashes if h in self.by_file), None) \
            or next((self.by_key[k] for k in keys if k in self.by_key), None)
        if wid is None:
            wid = "w" + secrets.token_hex(5)
            self.works[wid] = {
                "id": wid,
                "citekey": make_citekey(surnames, record.get("year"), record.get("title") or "", self.taken),
                "created": external.get("added") or datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "keys": [], "files": [],
                "meta": {**{k: record.get(k) for k in EDITABLE}, "source": record.get("source"),
                         "status": "imported",
                         "is_working_paper": record.get("type") in {"report", "posted-content"}},
                "description": record.get("abstract"), "tags": [],
                "user": {"read_status": "unread", "tags": [], "notes": ""},
            }
            self.works[wid]["meta"]["venue"] = venues.clean(record.get("venue"))
            self.taken.add(self.works[wid]["citekey"])
            self.created += 1
        else:
            self.joined += 1

        work = self.works[wid]
        user = work["user"]
        for key in keys:
            if key not in work["keys"]:
                work["keys"].append(key)
            self.by_key.setdefault(key, wid)
        for file_hash in file_hashes:
            if file_hash not in work["files"]:
                work["files"].append(file_hash)
            self.by_file[file_hash] = wid
        # dict.fromkeys keeps order and drops repeats: running the import twice
        # adds nothing the second time.
        user["tags"] = list(dict.fromkeys([*user.get("tags", []), *tags]))
        user["collections"] = list(dict.fromkeys([*user.get("collections", []), *collections]))
        for note in notes:
            if note not in user.get("notes", ""):
                user["notes"] = (user.get("notes", "") + "\n\n" + note).strip()
        user["zotero"] = external
        if external.get("citekey"):
            work["aliases"] = list(dict.fromkeys([*work.get("aliases", []), external["citekey"]]))
        return work

    def save(self) -> None:
        for wid, work in self.works.items():
            records.write_json(works_dir(self.cfg) / f"{wid}.json", work)
