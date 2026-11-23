"""Process one PDF end to end: read it, look it up, decide how far to trust it.

Result: a "read record", one JSON file per PDF content hash, stored in Dropbox
under <state>/reads/. It holds everything learned about the file, so the model
is paid for once per file, on any machine.

Statuses:
  verified      a registry record was found and agrees with what is printed
  extracted     a paper/book/chapter no registry confirmed: metadata is the
                model's transcription (typical for drafts and job market papers)
  needs-review  something does not add up; a person should look
  classified    not a library item (slides, problem set, ...): kind only
  confidential  in a no-LLM folder: only printed identifiers were used
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from . import extract, llm, naming, records, resolve
from .config import Config
from .records import LIBRARY_KINDS, final_metadata, known_topics, load_read, reads_dir  # noqa: F401

# An identifier-based record must look at least this similar to the printed
# title; otherwise the identifier probably belongs to some other paper.
ID_TITLE_FLOOR = 75


def _is_confidential(cfg: Config, rel: str) -> bool:
    return any(rel == p or rel.startswith(p.rstrip("/") + "/") for p in cfg.no_llm)


def _match_dict(match: resolve.Match, via: str) -> dict:
    return {
        "via": via,
        "record": match.record.to_dict(),
        "title_similarity": round(match.title_similarity, 1),
        "author_overlap": round(match.author_overlap, 2),
        "year_gap": match.year_gap,
    }


def process_file(cfg: Config, api, file_hash: str, rel: str,
                 known_topics: list[str] | None = None,
                 previous: dict | None = None, hint_ids: dict[str, str] | None = None,
                 refresh: bool = False) -> dict:
    """Run the whole pipeline for one file and save its read record.

    If `previous` (an earlier read record) already holds the model's reading,
    that reading is reused and only the registry lookup is redone, at no cost.
    `hint_ids` (the DOI of the page a PDF was saved from) fill in identifiers
    the PDF does not print; like printed ones, they are dropped if the record
    they lead to has a different title.
    """
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    info = extract.read_pdf(cfg.absolute(rel))
    # Identifiers: those printed on the pages, plus any in the file name ("w32515.pdf").
    ids = {**(hint_ids or {}), **naming.parse_filename(rel.rsplit("/", 1)[-1]).ids, **info.ids}

    result: dict = {
        "hash": file_hash, "path": rel, "processed_at": now,
        "pdf": {"pages": info.pages, "has_text": info.has_text, "ids": ids,
                "fingerprint": info.fingerprint},
        "reading": None, "registry": None, "status": "needs-review",
        "issues": [], "pending": [], "usage": {}, "model": None, "reused_reading": False,
    }

    def save() -> dict:
        records.write_json(reads_dir(cfg) / f"{file_hash}.json", result)
        return result

    if info.error:
        result["issues"].append(f"could not open PDF: {info.error}")
        return save()

    # --- Confidential folders: no text leaves the machine --------------------
    if _is_confidential(cfg, rel):
        record, via = resolve.by_ids(ids, refresh=refresh)
        result["status"] = "confidential"
        if record:
            result["registry"] = {"via": via, "record": record.to_dict()}
        return save()

    # --- Step 1: the model reads the opening pages ----------------------------
    if previous and previous.get("reading"):
        # Records made before volume/issue/pages were transcribed lack those keys.
        reading = llm.Reading(**{"volume": None, "issue": None, "pages": None, **previous["reading"]})
        result["usage"], result["model"] = previous.get("usage", {}), previous.get("model")
        result["reused_reading"] = True
    else:
        try:
            reading, usage = llm.read(
                api,
                text=info.text if info.has_text else None,
                image_png=None if info.has_text else extract.first_page_png(cfg.absolute(rel)),
                pages=info.pages, known_topics=known_topics or [],
            )
        except Exception as error:             # API failure, refusal, cut-off output
            result["issues"].append(f"reading failed: {type(error).__name__}: {error}")
            return save()
        result["usage"], result["model"] = usage, llm.MODEL
    result["reading"] = reading.model_dump()

    if reading.kind not in LIBRARY_KINDS:
        result["status"] = "classified"
        return save()

    title = reading.title or ""
    surnames = [a.family for a in reading.authors if a.family]

    # --- Step 2: look the identifier up, and distrust it if the title differs --
    by_id: resolve.Match | None = None
    try:
        record, via = resolve.by_ids(ids, refresh=refresh)
        if record is not None:
            by_id = resolve.check(record, title, surnames, reading.year)
            if title and by_id.title_similarity < ID_TITLE_FLOOR:
                result["issues"].append(
                    f"identifier {via} points to a different title: {record.title!r}")
                by_id = None

        # --- Step 3: search by title, unless the identifier already gave a
        #     published record. A working paper is searched too, to find the
        #     published version it turned into. ---------------------------------
        found: resolve.Match | None = None
        id_is_published = by_id is not None and resolve.is_published(by_id.record)
        if title and not id_is_published:
            found, skipped = resolve.best_match(
                title, surnames, reading.year, venue=reading.venue,
                is_working_paper=reading.is_working_paper, refresh=refresh)
            # A registry that could not be asked (quota used up) is noted, so
            # this file is looked up again once it is available.
            if found is None and skipped:
                result["pending"] = skipped
    except Exception as error:                 # registry unreachable: keep the reading
        result["issues"].append(f"registry lookup failed: {type(error).__name__}: {error}")
        result["status"] = "extracted" if title and reading.confidence != "low" else "needs-review"
        return save()

    # --- Step 4: choose the record and the status ------------------------------
    if found is not None and (by_id is None or resolve.is_published(found.record)):
        result["registry"] = _match_dict(found, "search")
        if by_id is not None:                  # keep the working-paper record as well
            result["working_paper"] = _match_dict(by_id, via)
    elif by_id is not None:
        result["registry"] = _match_dict(by_id, via)

    if result["registry"] is not None:
        result["status"] = "verified"
        # Same work, but only its working-paper record was found although the PDF
        # is the published article: venue and year would be wrong, so flag it.
        chosen = resolve.Record(**result["registry"]["record"])
        if not reading.is_working_paper and reading.venue and not resolve.is_published(chosen):
            result["status"] = "needs-review"
            result["issues"].append(
                "only a working-paper record was found for what looks like a published article")
        # Journals that publish "online first" register that date; the issue, and
        # the year everyone cites, can be one later. When the registry has no
        # print date and the PDF itself prints the following year, use the PDF's.
        record = result["registry"]["record"]
        if (record["online_date_only"] and not reading.is_working_paper
                and reading.year and record["year"] and reading.year == record["year"] + 1):
            result["registry"]["year_printed"] = reading.year
    elif title and reading.confidence != "low" and not result["issues"]:
        result["status"] = "extracted"
    else:
        result["status"] = "needs-review"
        if not title:
            result["issues"].append("no title could be read")
        if reading.confidence == "low":
            result["issues"].append("the model was not confident in its reading")
    return save()
