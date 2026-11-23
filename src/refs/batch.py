"""Resilient batch import: `.bib` files, lists of DOIs/URLs, and syllabus reading lists.

Design principle: decouple reconciliation from PDF acquisition.
  1. Parse the input (`.bib`, identifiers/URLs, or plain-text reading list).
  2. Preview against the existing library + Crossref/OpenAlex: papers already in
     refs are matched and simply tagged into the target collection; new papers
     show whether an open-access PDF was found or whether they will enter as
     verified `no-file` stubs.
  3. Commit: existing papers are tagged and new stubs are created immediately.
     Only reliable open-access PDFs (arXiv, NBER, OpenAlex OA, direct .pdf) are
     downloaded in the background; any blocked/paywalled paper stays cleanly as
     a stub with a one-click library-proxy link.
"""

from __future__ import annotations

import re
import sqlite3
from typing import TYPE_CHECKING

from . import bib, capture, collection, index, naming, reading, records, resolve, shared, venues, works

if TYPE_CHECKING:
    from .config import Config


# --- 1. Parsing inputs ------------------------------------------------------------------

_ENTRY_START = re.compile(r"@(\w+)\s*\{\s*([^,\s]+)\s*,", re.I)
_FIELD_NAME = re.compile(r"\s*([A-Za-z][A-Za-z0-9_-]*)\s*=\s*")
_HEADING_RE = re.compile(
    r"^(?:#{1,4}\s+(?:(?:week|lecture|module|topic|part|section)\s+\d*[:.\s-]+)?|"
    r"(?:week|lecture|module|topic|part|section)\s+\d*[:.\s-]+)(.+)$",
    re.I,
)
_PLAIN_CITE_RE = re.compile(
    r"^(?:[-*•\d.)]+\s+)?"
    r"(?P<authors>[A-Z][^()]{1,90}?)\s*"
    r"\((?P<year>(?:19|20)\d{2})[a-z]?\)[.,:\s]+"
    r"[\"“]?(?P<title>.+?)[\"”]?\.?$"
)


def _read_braced(text: str, pos: int) -> tuple[str, int]:
    """Read a `{...}` or `"..."` or bare BibTeX value starting at `pos`."""
    n = len(text)
    while pos < n and text[pos].isspace():
        pos += 1
    if pos >= n:
        return "", pos
    if text[pos] == "{":
        depth = 0
        start = pos + 1
        while pos < n:
            if text[pos] == "{":
                depth += 1
            elif text[pos] == "}":
                depth -= 1
                if depth == 0:
                    return text[start:pos], pos + 1
            pos += 1
        return text[start:], n
    if text[pos] == '"':
        pos += 1
        start = pos
        while pos < n and text[pos] != '"':
            if text[pos] == "\\" and pos + 1 < n:
                pos += 2
                continue
            pos += 1
        return text[start:pos], min(n, pos + 1)
    start = pos
    while pos < n and text[pos] not in ",}\n":
        pos += 1
    return text[start:pos].strip(), pos


def _clean_bib_val(val: str) -> str:
    val = re.sub(r"[{}]", "", val)
    return re.sub(r"\s+", " ", val).strip()


def _parse_bib_authors(raw: str) -> list[dict]:
    out = []
    for part in re.split(r"\s+and\s+", raw.strip(), flags=re.I):
        part = _clean_bib_val(part)
        if not part:
            continue
        if "," in part:
            family, given = [x.strip() for x in part.split(",", 1)]
        else:
            words = part.split()
            given, family = " ".join(words[:-1]), words[-1]
        if family:
            out.append({"given": given, "family": family})
    return out


def parse_bibtex(text: str) -> list[dict]:
    """Parse BibTeX entries into raw import items."""
    starts = list(_ENTRY_START.finditer(text))
    items: list[dict] = []
    for idx, match in enumerate(starts):
        entry_type = match.group(1).lower()
        if entry_type in {"string", "comment", "preamble"}:
            continue
        end = starts[idx + 1].start() if idx + 1 < len(starts) else len(text)
        body = text[match.end():end]
        fields: dict[str, str] = {}
        pos = 0
        while pos < len(body):
            m = _FIELD_NAME.match(body, pos)
            if not m:
                pos += 1
                continue
            name = m.group(1).lower()
            val, pos = _read_braced(body, m.end())
            fields[name] = _clean_bib_val(val)
            while pos < len(body) and body[pos] in " \t\r\n,":
                pos += 1
        doi = (fields.get("doi") or "").lower().removeprefix("https://doi.org/").removeprefix("http://dx.doi.org/")
        ids: dict[str, str] = {}
        if doi:
            ids["doi"] = doi
        if eprint := fields.get("eprint"):
            if re.fullmatch(r"\d{4}\.\d{4,5}(?:v\d+)?", eprint):
                ids["arxiv"] = re.sub(r"v\d+$", "", eprint)
        url = fields.get("url") or ""
        if url:
            parsed = capture.parse(url)
            for k, v in parsed.ids.items():
                ids.setdefault(k, v)
        year_str = fields.get("year") or ""
        year = int(m_yr.group(0)) if (m_yr := re.search(r"\b(19|20)\d{2}\b", year_str)) else None
        items.append({
            "raw": match.group(2),
            "title": fields.get("title") or "",
            "authors": _parse_bib_authors(fields.get("author") or ""),
            "year": year,
            "venue": fields.get("journal") or fields.get("booktitle") or fields.get("publisher") or None,
            "volume": fields.get("volume") or None,
            "issue": fields.get("number") or None,
            "pages": fields.get("pages") or None,
            "type": "book" if entry_type == "book" else "book-chapter" if entry_type == "incollection"
                    else "journal-article" if entry_type == "article" else "posted-content",
            "ids": ids,
            "url": url or None,
            "topic": None,
        })
    return items


def _parse_plain_authors(raw: str) -> list[dict]:
    # "Berry, Levinsohn and Pakes" or "O'Donoghue & Rabin" -> surname list
    cleaned = re.sub(r"\bet\s+al\.?", "", raw, flags=re.I)
    parts = [p.strip(" .") for p in re.split(r"\s*(?:,|&|\band\b)\s*", cleaned) if p.strip(" .")]
    out = []
    for p in parts:
        words = p.split()
        if not words:
            continue
        # Skip single-letter initials standing alone ("Berry, S., Levinsohn, J.")
        if len(words) == 1 and len(words[0].rstrip(".")) == 1:
            continue
        out.append({"given": " ".join(words[:-1]), "family": words[-1]})
    return out


def _llm_parse_syllabus(text: str) -> list[dict]:
    """Use Claude Haiku to parse an unstructured syllabus reading list."""
    from pydantic import BaseModel
    from . import keys, llm

    if not keys.get_api_key():
        return []

    class SyllabusEntry(BaseModel):
        title: str
        authors: list[str]
        year: int | None = None
        topic: str | None = None

    class SyllabusParsed(BaseModel):
        items: list[SyllabusEntry]

    api = llm.client()
    response = api.messages.parse(
        model=llm.MODEL,
        max_tokens=8000,
        system=(
            "Extract academic papers, books, or chapters from this syllabus or reading list. "
            "For each item, return its title, author surnames (in order), year (if present), "
            "and the section/topic heading it falls under (short lowercase label, or null). "
            "Skip course logistics, office hours, and grading notes."
        ),
        messages=[{"role": "user", "content": text[:20000]}],
        output_format=SyllabusParsed,
        output_config={"effort": "low"},
    )
    if not response.parsed_output:
        return []
    out = []
    for entry in response.parsed_output.items:
        if not entry.title.strip():
            continue
        out.append({
            "raw": entry.title.strip(),
            "title": entry.title.strip(),
            "authors": [{"given": "", "family": a.strip()} for a in entry.authors if a.strip()],
            "year": entry.year,
            "venue": None,
            "ids": {},
            "url": None,
            "topic": entry.topic.strip() if entry.topic else None,
        })
    return out


def parse_input(text: str, *, use_llm: bool = False) -> list[dict]:
    """Parse `.bib`, DOI/URL lines, or a plain-text syllabus into raw items."""
    text = (text or "").strip()
    if not text:
        return []
    if _ENTRY_START.search(text):
        return parse_bibtex(text)

    items: list[dict] = []
    unparsed_lines: list[str] = []
    current_topic: str | None = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if (hm := _HEADING_RE.match(line)) and not re.search(r"\b(?:19|20)\d{2}\b", line):
            current_topic = hm.group(1).strip(" :-")
            continue
        # Strip leading markdown bullets for URL/DOI detection.
        bare = re.sub(r"^[-*•\d.)]+\s+", "", line).strip()
        target = capture.parse(bare)
        if target.ids or target.pdf_url or target.page_url:
            items.append({
                "raw": bare,
                "title": "",
                "authors": [],
                "year": None,
                "venue": None,
                "ids": dict(target.ids),
                "url": target.pdf_url or target.page_url,
                "pdf_urls": list(target.pdf_urls),
                "topic": current_topic,
            })
            continue
        if m := _PLAIN_CITE_RE.match(line):
            title = m.group("title").strip(" .\"“”")
            # Trim trailing journal/venue after a sentence break if title has a clear period.
            title = title.split(". ", 1)[0].strip()
            items.append({
                "raw": line,
                "title": title,
                "authors": _parse_plain_authors(m.group("authors")),
                "year": int(m.group("year")),
                "venue": None,
                "ids": {},
                "url": None,
                "topic": current_topic,
            })
            continue
        unparsed_lines.append(line)

    if use_llm and (unparsed_lines and not items or len(unparsed_lines) >= 3):
        llm_items = _llm_parse_syllabus(text)
        if llm_items:
            return llm_items
    return items


# --- 2. Preview & reconcile against existing library ------------------------------------

def _existing_lookup(all_works: dict[str, dict]) -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
    by_doi: dict[str, str] = {}
    by_title_author: dict[str, str] = {}
    by_title: dict[str, str] = {}
    for wid, w in all_works.items():
        meta = w.get("meta") or {}
        if doi := (meta.get("doi") or "").lower():
            by_doi[doi] = wid
        for key in w.get("keys") or []:
            if key.startswith("doi:"):
                by_doi.setdefault(key[4:].lower(), wid)
        title = bib._norm_title(meta.get("title") or "")
        if title:
            by_title.setdefault(title, wid)
            surnames = [a["family"] for a in meta.get("authors") or [] if a.get("family")]
            if surnames:
                by_title_author.setdefault(f"{title}|{naming.slugify(surnames[0])}", wid)
    return by_doi, by_title_author, by_title


def _match_existing(record: dict, by_doi: dict, by_title_author: dict, by_title: dict) -> str | None:
    if doi := (record.get("doi") or "").lower():
        if wid := by_doi.get(doi):
            return wid
    title = bib._norm_title(record.get("title") or "")
    if not title:
        return None
    surnames = [a["family"] for a in record.get("authors") or [] if a.get("family")]
    if surnames:
        if wid := by_title_author.get(f"{title}|{naming.slugify(surnames[0])}"):
            return wid
    return by_title.get(title)


def preview(cfg: Config, conn: sqlite3.Connection, entries: list[dict],
            *, resolve_online: bool = True) -> dict:
    """Reconcile parsed entries against the library and registries without writing anything."""
    all_works = works.load_all_readonly(cfg)
    by_doi, by_title_author, by_title = _existing_lookup(all_works)
    out: list[dict] = []

    for idx, entry in enumerate(entries):
        ids = dict(entry.get("ids") or {})
        url = entry.get("url")
        topic = entry.get("topic")
        topic_slug = collection.topic_part(topic) if topic else None

        # First try matching raw entry directly against existing library (zero network).
        raw_rec = {
            "title": entry.get("title") or "",
            "authors": entry.get("authors") or [],
            "year": entry.get("year"),
            "venue": entry.get("venue"),
            "type": entry.get("type") or "journal-article",
            "doi": ids.get("doi"),
            "volume": entry.get("volume"),
            "issue": entry.get("issue"),
            "pages": entry.get("pages"),
            "source": "import",
        }
        existing_id = _match_existing(raw_rec, by_doi, by_title_author, by_title)
        if existing_id is None and ids.get("nber"):
            existing_id = by_doi.get(f"10.3386/w{ids['nber']}")
        if existing_id is None and ids.get("arxiv"):
            existing_id = by_doi.get(f"10.48550/arxiv.{ids['arxiv']}")

        if existing_id is not None:
            w = all_works[existing_id]
            meta = w.get("meta") or {}
            out.append({
                "idx": idx,
                "status": "existing",
                "work_id": existing_id,
                "citekey": w.get("citekey"),
                "title": meta.get("title") or raw_rec["title"],
                "authors": [a["family"] for a in meta.get("authors") or [] if a.get("family")],
                "year": meta.get("year"),
                "venue": venues.label(meta),
                "topic": topic_slug,
                "collections": collection.explicit(w),
                "pdf_urls": [],
                "record": None,
                "source_url": url,
            })
            continue

        # Not in library yet: try resolving metadata & OA PDF links.
        resolved_rec: dict | None = None
        pdf_urls: list[str] = list(entry.get("pdf_urls") or [])
        if resolve_online:
            try:
                target = capture.Target(ids=dict(ids), page_url=url if url and not pdf_urls else None,
                                        pdf_urls=list(pdf_urls))
                if target.pdf_urls:
                    target.pdf_url = target.pdf_urls[0]
                capture.discover(target, fetch_page=bool(url and not ids and not raw_rec["title"]))
                ids.update(target.ids)
                pdf_urls = list(target.pdf_urls)

                rec_obj, _ = resolve.by_ids(ids) if ids else (None, None)
                if rec_obj is None and raw_rec["title"]:
                    surnames = [a["family"] for a in raw_rec["authors"] if a.get("family")]
                    match, _ = resolve.best_match(raw_rec["title"], surnames, raw_rec["year"],
                                                  venue=raw_rec["venue"])
                    if match is not None:
                        rec_obj = match.record
                if rec_obj is not None:
                    resolved_rec = rec_obj.to_dict()
                    resolved_rec["venue"] = venues.clean(resolved_rec.get("venue"))
                    if resolved_rec.get("doi") and not pdf_urls:
                        pdf_urls = capture.open_copies(resolved_rec["doi"])
            except Exception:  # noqa: BLE001 (keep batch resilient)
                resolved_rec = None

        final_rec = resolved_rec or (raw_rec if raw_rec["title"] else None)
        if final_rec is not None:
            # Re-check against existing works after resolution (e.g. a URL resolved to a paper we own).
            if (existing_id := _match_existing(final_rec, by_doi, by_title_author, by_title)) is not None:
                w = all_works[existing_id]
                meta = w.get("meta") or {}
                out.append({
                    "idx": idx,
                    "status": "existing",
                    "work_id": existing_id,
                    "citekey": w.get("citekey"),
                    "title": meta.get("title") or final_rec.get("title"),
                    "authors": [a["family"] for a in meta.get("authors") or [] if a.get("family")],
                    "year": meta.get("year"),
                    "venue": venues.label(meta),
                    "topic": topic_slug,
                    "collections": collection.explicit(w),
                    "pdf_urls": [],
                    "record": None,
                    "source_url": url,
                })
                continue

            status = "new_oa" if pdf_urls else "new_stub"
            source_url = url or (f"https://doi.org/{final_rec['doi']}" if final_rec.get("doi") else None)
            out.append({
                "idx": idx,
                "status": status,
                "work_id": None,
                "citekey": None,
                "title": final_rec.get("title"),
                "authors": [a["family"] for a in final_rec.get("authors") or [] if a.get("family")],
                "year": final_rec.get("year"),
                "venue": final_rec.get("venue"),
                "doi": final_rec.get("doi"),
                "topic": topic_slug,
                "pdf_urls": pdf_urls,
                "record": final_rec,
                "source_url": source_url,
            })
        else:
            out.append({
                "idx": idx,
                "status": "unresolved",
                "work_id": None,
                "citekey": None,
                "title": entry.get("raw") or url or "",
                "authors": [],
                "year": None,
                "venue": None,
                "topic": topic_slug,
                "pdf_urls": pdf_urls,
                "record": None,
                "source_url": url,
            })

    counts = {k: sum(1 for i in out if i["status"] == k)
              for k in ("existing", "new_oa", "new_stub", "unresolved")}
    return {"items": out, "counts": counts}


# --- 3. Commit selected items -----------------------------------------------------------

def commit(cfg: Config, conn: sqlite3.Connection, items: list[dict], *,
           collection_name: str | None = None, queue: bool = False) -> dict:
    """Tag existing papers and create verified stubs for new ones immediately.

    Returns `download_targets` (OA PDF URLs + target collections) for the caller
    to fetch sequentially in the background; failures leave clean stubs behind.
    """
    base_col = collection.clean_name(collection_name) if collection_name else None
    tagged: list[str] = []
    created: list[dict] = []
    download_targets: list[dict] = []

    for item in items:
        status = item.get("status")
        topic = item.get("topic")
        target_col = f"{base_col}/{topic}" if (base_col and topic) else base_col
        cols = [target_col] if target_col else []

        if status == "existing" and item.get("work_id"):
            wid = item["work_id"]
            if cols:
                shared.collect(cfg, conn, [wid], cols)
            if queue:
                work = works.load_one(cfg, wid)
                if work is not None:
                    reading.set_queued(work, True, for_collection=base_col)
                    records.write_json(works.works_dir(cfg) / f"{wid}.json", work)
                    conn.execute("UPDATE files SET queued=? WHERE work_id=?",
                                 (work["user"].get("queued"), wid))
                    conn.commit()
            tagged.append(wid)
        elif status in {"new_oa", "new_stub"} and item.get("record"):
            work = works.create_stub(cfg, item["record"], item.get("source_url"))
            wid = work["id"]
            if cols:
                shared.collect(cfg, conn, [wid], cols)
            if queue:
                work = works.load_one(cfg, wid) or work
                reading.set_queued(work, True, for_collection=base_col)
                records.write_json(works.works_dir(cfg) / f"{wid}.json", work)
            created.append({"work_id": wid, "citekey": work["citekey"], "title": item.get("title")})
            if item.get("pdf_urls"):
                download_targets.append({
                    "work_id": wid,
                    "citekey": work["citekey"],
                    "title": item.get("title") or work["citekey"],
                    "pdf_urls": item["pdf_urls"],
                    "collections": cols,
                    "doi": (item.get("record") or {}).get("doi"),
                })

    index.refresh_derived(cfg, conn)
    conn.commit()
    return {
        "tagged": len(tagged),
        "created": len(created),
        "stubs": created,
        "download_targets": download_targets,
    }
