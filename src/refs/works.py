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
import os
import re
import secrets
import threading
from datetime import datetime, timezone
from pathlib import Path

from . import naming, records, venues
from .config import Config

_CITEKEY_SKIP = naming.STOPWORDS | {"is", "are", "do", "does", "how", "why", "what", "when", "some"}
_STATUS_RANK = {"verified": 0, "extracted": 1, "needs-review": 2}

# Per-file stat-validated cache of parsed work dicts for read-only callers:
# works_dir -> {filename: ((ino, size, mtime_ns), work_dict)}
_readonly_cache: dict[str, dict[str, tuple[tuple[int, int, int], dict]]] = {}
_readonly_lock = threading.Lock()


def works_dir(cfg: Config) -> Path:
    return cfg.absolute(cfg.state_dir) / "works"


def load_one(cfg: Config, work_id: str) -> dict | None:
    """Load a single work by id without scanning the whole works folder."""
    if not work_id or "/" in work_id or "\\" in work_id or ".." in work_id or work_id.startswith("."):
        return None
    return records.read_json(works_dir(cfg) / f"{work_id}.json")


def load_all(cfg: Config) -> dict[str, dict]:
    """Every work, keyed by id (fresh mutable dicts, stat-cached against disk)."""
    folder = works_dir(cfg)
    if not folder.is_dir():
        return {}
    out: dict[str, dict] = {}
    try:
        with os.scandir(folder) as entries:
            for entry in entries:
                if not entry.name.endswith(".json") or entry.name.startswith("."):
                    continue
                try:
                    st = entry.stat()
                except OSError:
                    continue
                work = records.read_json(Path(entry.path), _stat=st)
                if work and "id" in work:
                    out[work["id"]] = work
    except FileNotFoundError:
        pass
    return out


def load_all_readonly(cfg: Config) -> dict[str, dict]:
    """Every work, keyed by id, for callers that only read the dicts.

    Stat-validates every file in works/ via a single os.scandir and reuses the
    already-parsed dicts for unchanged files (zero disk reads and zero JSON
    re-parsing when nothing changed). Callers MUST NOT mutate returned dicts.
    """
    # Respect tests that monkeypatch works.load_all.
    if load_all is not _ORIGINAL_LOAD_ALL:
        return load_all(cfg)
    folder = works_dir(cfg)
    if not folder.is_dir():
        return {}
    dir_key = str(folder)
    with _readonly_lock:
        prev = _readonly_cache.get(dir_key, {})
    fresh: dict[str, tuple[tuple[int, int, int], dict]] = {}
    out: dict[str, dict] = {}
    try:
        with os.scandir(folder) as entries:
            for entry in entries:
                if not entry.name.endswith(".json") or entry.name.startswith("."):
                    continue
                try:
                    st = entry.stat()
                except OSError:
                    continue
                sig = (st.st_ino, st.st_size, st.st_mtime_ns)
                cached = prev.get(entry.name)
                if cached is not None and cached[0] == sig:
                    work = cached[1]
                else:
                    work = records.read_json(Path(entry.path), _stat=st)
                    if not work or "id" not in work:
                        continue
                fresh[entry.name] = (sig, work)
                out[work["id"]] = work
    except FileNotFoundError:
        pass
    with _readonly_lock:
        _readonly_cache[dir_key] = fresh
    return out


_ORIGINAL_LOAD_ALL = load_all


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
        # Registries are sometimes incomplete (a first page with no end, no
        # volume). Where the PDF is the published version and prints the missing
        # detail, take it from the PDF, and say so in `from_pdf`.
        meta["from_pdf"] = []
        if not reading.get("is_working_paper"):
            for key in ("volume", "issue"):
                if not meta.get(key) and reading.get(key):
                    meta[key] = str(reading[key])
                    meta["from_pdf"].append(key)
            printed = (reading.get("pages") or "").replace("–", "-").replace(" ", "")
            registered = (meta.get("pages") or "").strip()
            is_range = re.fullmatch(r"\w*\d+-\w*\d+", printed) is not None
            # Only complete a range that starts on the page the registry gives.
            if is_range and "-" not in registered and (not registered or printed.startswith(registered + "-")):
                meta["pages"] = printed
                meta["from_pdf"].append("pages")
    else:
        meta = {
            "title": reading.get("title"), "authors": reading.get("authors") or [],
            "year": reading.get("year"), "venue": reading.get("venue"),
            "type": None, "doi": None, "volume": reading.get("volume"),
            "issue": reading.get("issue"), "pages": reading.get("pages"),
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


def override_keys(work: dict) -> list[str]:
    """Keys derived from user corrections (DOI, or overridden title/authors).

    Added to `work["keys"]` so a PDF arriving later with the corrected DOI or
    title attaches to this work instead of creating a second one.
    """
    overrides = (work.get("user") or {}).get("overrides") or {}
    if not overrides:
        return []
    meta = work.get("meta") or {}
    keys: list[str] = []
    if doi := (overrides.get("doi") or "").strip().lower():
        keys.append(f"doi:{doi}")
    if "title" in overrides or "authors" in overrides:
        title = overrides.get("title") or meta.get("title") or ""
        authors = overrides.get("authors") or meta.get("authors") or []
        surnames = [a["family"] for a in authors if a.get("family")]
        if title:
            first = naming.slugify(surnames[0]) if surnames else ""
            keys.append(f"title:{naming.slugify(title)[:80]}|{first}")
    return keys


def sync_keys(work: dict) -> None:
    """Ensure override-derived keys are present on `work["keys"]`."""
    have = work.setdefault("keys", [])
    for key in override_keys(work):
        if key not in have:
            have.append(key)


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
    sync_keys(work)


def sync(cfg: Config) -> dict[str, str]:
    """Bring the work files in line with the read records.

    Returns a map from file hash to work id. Safe to run any number of times:
    a file stays in the work it is already in, ids and citekeys never change.
    """
    all_reads = records.load_all_reads(cfg)
    if not all_reads:
        return {}
    reads = {
        h: read for h, read in all_reads.items()
        if (read.get("reading") or {}).get("kind") in records.LIBRARY_KINDS
    }

    works = load_all(cfg)
    before = {wid: json.dumps(w, sort_keys=True) for wid, w in works.items()}
    by_file = {h: wid for wid, w in works.items() for h in w["files"]}
    by_key = {k: wid for wid, w in works.items() for k in [*w["keys"], *override_keys(w)]}
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
        if len(candidates) == 1:
            best = candidates[0]
            best_meta = _meta_from(best)
        else:
            scored = [(_STATUS_RANK.get(r["status"], 9), (m := _meta_from(r))["is_working_paper"], r, m)
                      for r in candidates]
            _, _, best, best_meta = min(scored, key=lambda item: (item[0], item[1]))
        work["meta"] = best_meta
        apply_user_edits(work)
        reading = best.get("reading") or {}
        work["description"] = reading.get("description")
        hidden = {t.lower() for t in (work["user"].get("hidden_tags") or [])}
        raw_tags = [*reading.get("fields", []), *reading.get("methods", []),
                    *reading.get("topics", [])]
        work["tags"] = [t for t in raw_tags if t.lower() not in hidden]

    # 3. Keys that project .bib files use for these works (bibkeys.py).
    from . import bibkeys
    bibkeys.learn(cfg, works)

    # 4. Write only the files that changed (keeps Dropbox quiet).
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
        # Zotero's names ("Card-Dahl style") become names refs uses ("card-dahl-style").
        from .collection import explicit
        user["collections"] = explicit({"user": {"collections": [*user.get("collections", []), *collections]}})
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


# --- User tag editing, merging and removing works -------------------------------------

def _clean_tag(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip())


def edit_tags(cfg: Config, work_id: str, *, add_user: list[str] = (),
              remove_user: list[str] = (), hide_auto: list[str] = (),
              restore_auto: bool = False) -> dict:
    """Add or remove personal tags (`user.tags`), or hide/restore model tags.

    Hidden model tags are stored in `user.hidden_tags` so `sync()` does not
    re-add them on the next scan. Returns the updated work.
    """
    work = load_one(cfg, work_id)
    if work is None:
        raise ValueError(f"no work {work_id}")
    user = work.setdefault("user", {})
    mine = list(user.get("tags") or [])
    mine_lower = {t.lower() for t in mine}
    for raw in add_user:
        tag = _clean_tag(raw)
        if tag and tag.lower() not in mine_lower:
            mine.append(tag)
            mine_lower.add(tag.lower())
    drop_user = {t.strip().lower() for t in remove_user if t.strip()}
    if drop_user:
        mine = [t for t in mine if t.lower() not in drop_user]
    user["tags"] = mine

    hidden = list(user.get("hidden_tags") or [])
    hidden_lower = {t.lower() for t in hidden}
    if restore_auto:
        hidden, hidden_lower = [], set()
    else:
        for raw in hide_auto:
            tag = _clean_tag(raw)
            if tag and tag.lower() not in hidden_lower:
                hidden.append(tag)
                hidden_lower.add(tag.lower())
    if hidden:
        user["hidden_tags"] = hidden
    else:
        user.pop("hidden_tags", None)

    if restore_auto:
        # Rebuild model tags from the work's read records if available.
        all_reads = [r for h in work.get("files") or [] if (r := records.load_read(cfg, h)) is not None]
        if all_reads:
            best = min(all_reads, key=lambda r: (_STATUS_RANK.get(r["status"], 9),
                                                 _meta_from(r)["is_working_paper"]))
            reading_rec = best.get("reading") or {}
            work["tags"] = [*reading_rec.get("fields", []), *reading_rec.get("methods", []),
                            *reading_rec.get("topics", [])]
    else:
        work["tags"] = [t for t in (work.get("tags") or []) if t.lower() not in hidden_lower]

    records.write_json(works_dir(cfg) / f"{work_id}.json", work)
    return work


def merge(cfg: Config, keep_id: str, drop_id: str) -> dict:
    """Merge `drop_id` into `keep_id`: files, keys, collections, tags, read
    history, queue and notes all move onto `keep_id`, and `drop_id` is moved
    to `<state>/trash/`. `drop_id`'s citekey stays in `keep_id`'s aliases so
    existing references still resolve."""
    from . import collection, notes, reading

    if keep_id == drop_id:
        raise ValueError("cannot merge a work into itself")
    keep = load_one(cfg, keep_id)
    drop = load_one(cfg, drop_id)
    if keep is None or drop is None:
        raise ValueError("one of those works no longer exists")

    for h in drop.get("files") or []:
        if h not in keep["files"]:
            keep["files"].append(h)
    for k in [*drop.get("keys", []), *override_keys(drop)]:
        if k not in keep["keys"]:
            keep["keys"].append(k)
    aliases = [*keep.get("aliases", []), *drop.get("aliases", [])]
    if drop.get("citekey") and drop["citekey"] != keep.get("citekey"):
        aliases.append(drop["citekey"])
    if aliases:
        keep["aliases"] = list(dict.fromkeys(aliases))

    ku, du = keep.setdefault("user", {}), drop.get("user") or {}
    # Collections, personal tags, hidden tags, former names, Zotero notes.
    cols = collection.cleaned([*collection.explicit(keep), *collection.explicit(drop)])
    if cols:
        ku["collections"] = cols
    tags = list(dict.fromkeys([*(ku.get("tags") or []), *(du.get("tags") or [])]))
    ku["tags"] = tags
    if hidden := list(dict.fromkeys([*(ku.get("hidden_tags") or []), *(du.get("hidden_tags") or [])])):
        ku["hidden_tags"] = hidden
    if former := list(dict.fromkeys([*(ku.get("former_names") or []), *(du.get("former_names") or [])])):
        ku["former_names"] = former
    if du.get("notes") and du["notes"] not in (ku.get("notes") or ""):
        ku["notes"] = ((ku.get("notes") or "") + "\n\n" + du["notes"]).strip()
    if not ku.get("source_url") and du.get("source_url"):
        ku["source_url"] = du["source_url"]
    if du.get("reads"):
        existing_reads = list(ku.get("reads") or [])
        for entry in du["reads"]:
            if entry not in existing_reads:
                existing_reads.append(entry)
        existing_reads.sort(key=lambda r: r.get("at") or "")
        ku["reads"] = existing_reads

    # Read status: keep the higher effective status (manual wins if set).
    k_stat, _ = reading.effective(keep)
    d_stat, _ = reading.effective(drop)
    if reading.RANK.get(d_stat, 0) > reading.RANK.get(k_stat, 0):
        if du.get("read_set"):
            ku["read_status"] = du.get("read_status", d_stat)
            ku["read_set"] = du["read_set"]
    if drop.get("read_auto"):
        k_auto = (keep.get("read_auto") or {}).get("status")
        d_auto = drop["read_auto"].get("status")
        if reading.RANK.get(d_auto, -1) > reading.RANK.get(k_auto, -1):
            keep["read_auto"] = drop["read_auto"]

    # Queue: preserve queue info if keep was not queued.
    if not ku.get("queued") and du.get("queued"):
        for qkey in ("queued", "queue_note", "queue_for", "queue_priority"):
            if qkey in du:
                ku[qkey] = du[qkey]

    # Obsidian notes: if drop has a note and keep does not, copy drop's note body over.
    note_status = "none"
    if notes.enabled(cfg):
        k_note = notes.read(cfg, keep)
        d_note = notes.read(cfg, drop)
        if d_note["exists"] and d_note["body"].strip():
            if not k_note["exists"] or not k_note["body"].strip():
                notes.write(cfg, keep, d_note["body"], k_note["version"])
                note_status = "copied"
            else:
                note_status = "both"

    records.write_json(works_dir(cfg) / f"{keep_id}.json", keep)
    trash_dir = cfg.absolute(cfg.state_dir) / "trash"
    trash_dir.mkdir(parents=True, exist_ok=True)
    drop_file = works_dir(cfg) / f"{drop_id}.json"
    if drop_file.exists():
        drop_file.replace(trash_dir / f"merged-{drop_id}.json")
    return {"work": keep, "dropped": drop_id, "dropped_citekey": drop.get("citekey"),
            "note_status": note_status}


def remove(cfg: Config, conn, work_id: str, *, trash_files: bool = True) -> dict:
    """Remove a work from the library: move its PDFs (if any and `trash_files`)
    to the macOS Trash and archive its work record in `<state>/trash/`.
    Managed folders (canvas-sync, Git, Overleaf) are skipped rather than touched."""
    from . import library, organize, rename

    work = load_one(cfg, work_id)
    if work is None:
        raise ValueError(f"no work {work_id}")
    paths = library.paths_of(conn, work_id)
    trashed: list[str] = []
    skipped: list[tuple[str, str]] = []
    if trash_files and paths:
        for rel in paths:
            if reason := rename.blocked_reason(cfg, rel):
                skipped.append((rel, reason))
                continue
            full = cfg.absolute(rel)
            if full.exists():
                rename.to_trash(full)
                trashed.append(rel)
        if paths and not trashed and skipped:
            raise organize.Refused(f"cannot remove files: {skipped[0][1]}")

    trash_dir = cfg.absolute(cfg.state_dir) / "trash"
    trash_dir.mkdir(parents=True, exist_ok=True)
    work_file = works_dir(cfg) / f"{work_id}.json"
    if work_file.exists():
        work_file.replace(trash_dir / f"{work_id}.json")
    conn.execute("DELETE FROM files WHERE hash=?", (f"work:{work_id}",))
    conn.execute("DELETE FROM search WHERE hash=?", (f"work:{work_id}",))
    conn.commit()
    return {"id": work_id, "citekey": work.get("citekey"), "trashed": trashed,
            "skipped": [{"path": p, "why": why} for p, why in skipped]}
