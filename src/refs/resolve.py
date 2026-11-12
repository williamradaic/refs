"""Ask the registries: given an identifier, return the paper's metadata.

This is where bibliographic *facts* come from. Crossref holds DOIs for journal
articles and NBER working papers; arXiv has its own service. Responses are
cached on this machine, so each identifier is fetched once.

This module currently resolves by identifier only. Searching by title (for
PDFs with no identifier on them) arrives with the LLM step, which supplies the
title to search for.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from pathlib import Path

import httpx

CACHE_DIR = Path.home() / "Library" / "Caches" / "refs" / "http"
# A generic client name. Registries answer faster when given a contact email,
# but none is sent unless you add one yourself (a later config option).
USER_AGENT = "refs/0.1 (personal reference manager)"
PAUSE_SECONDS = 0.15      # be gentle: a short pause between live requests


@dataclass
class Record:
    """Metadata for one work, as a registry states it."""

    title: str
    authors: list[dict] = field(default_factory=list)   # [{"given": ..., "family": ...}]
    year: int | None = None
    venue: str | None = None        # journal or series
    type: str | None = None         # journal-article, report, book-chapter, ...
    doi: str | None = None
    volume: str | None = None
    issue: str | None = None
    pages: str | None = None
    publisher: str | None = None
    abstract: str | None = None
    source: str = ""                # "crossref" or "arxiv": where the facts came from
    authors_source: str = ""        # set when the authors came from a second registry

    @property
    def surnames(self) -> list[str]:
        return [a.get("family", "") for a in self.authors if a.get("family")]

    def to_dict(self) -> dict:
        return asdict(self)


# --- HTTP with an on-disk cache -------------------------------------------------

def _get(url: str, params: dict | None = None) -> httpx.Response | None:
    """GET a URL, returning None on 404. Successful bodies are cached forever."""
    key = hashlib.sha1((url + json.dumps(params or {}, sort_keys=True)).encode()).hexdigest()
    cached = CACHE_DIR / key
    if cached.exists():
        body = cached.read_bytes()
        if body == b"404":
            return None
        return httpx.Response(200, content=body)

    time.sleep(PAUSE_SECONDS)
    response = httpx.get(
        url, params=params, headers={"User-Agent": USER_AGENT},
        timeout=30, follow_redirects=True,
    )
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    if response.status_code == 404:
        cached.write_bytes(b"404")     # remember misses too
        return None
    response.raise_for_status()         # other errors (rate limit, outage) are not cached
    cached.write_bytes(response.content)
    return response


# --- Crossref ---------------------------------------------------------------------

def _strip_tags(text: str) -> str:
    """Crossref titles and abstracts can contain markup such as <i> or <jats:p>."""
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text)).strip()


def _from_crossref(msg: dict) -> Record:
    # Several date fields exist; the earliest publication date is the citable year.
    year = None
    for key in ("published-print", "published", "issued", "published-online"):
        parts = (msg.get(key) or {}).get("date-parts") or [[None]]
        if parts[0] and parts[0][0]:
            year = int(parts[0][0])
            break
    title = _strip_tags(" ".join(msg.get("title") or []))
    if msg.get("subtitle"):
        title += ": " + _strip_tags(" ".join(msg["subtitle"]))
    return Record(
        title=title,
        authors=[
            {"given": a.get("given", ""), "family": a.get("family") or a.get("name", "")}
            for a in msg.get("author") or []
        ],
        year=year,
        venue=_strip_tags(" ".join(msg.get("container-title") or [])) or None,
        type=msg.get("type"),
        doi=(msg.get("DOI") or "").lower() or None,
        volume=msg.get("volume"),
        issue=msg.get("issue"),
        pages=msg.get("page"),
        publisher=msg.get("publisher"),
        abstract=_strip_tags(msg["abstract"]) if msg.get("abstract") else None,
        source="crossref",
    )


def _openalex_authors(doi: str) -> list[dict]:
    """Author list for a DOI from OpenAlex, as a fallback.

    OpenAlex gives full display names ("Henrik Jacobsen Kleven"); the last word
    is taken as the surname, which is right for most but not all names.
    """
    response = _get(f"https://api.openalex.org/works/doi:{doi}")
    if response is None:
        return []
    authors = []
    for item in response.json().get("authorships") or []:
        name = ((item.get("author") or {}).get("display_name") or "").strip()
        if name:
            given, _, family = name.rpartition(" ")
            authors.append({"given": given, "family": family})
    return authors


def by_doi(doi: str) -> Record | None:
    """Look a DOI up in Crossref. None if Crossref does not know it."""
    response = _get(f"https://api.crossref.org/works/{doi}")
    if response is None:
        return None
    record = _from_crossref(response.json()["message"])
    # Crossref has no author list for some publishers (Econometrica is one).
    if not record.authors:
        record.authors = _openalex_authors(doi)
        if record.authors:
            record.authors_source = "openalex"
    return record


# --- arXiv ------------------------------------------------------------------------

_ATOM = {"a": "http://www.w3.org/2005/Atom"}


def by_arxiv(arxiv_id: str) -> Record | None:
    response = _get("https://export.arxiv.org/api/query", {"id_list": arxiv_id})
    if response is None:
        return None
    entry = ET.fromstring(response.text).find("a:entry", _ATOM)
    if entry is None or entry.find("a:title", _ATOM) is None:
        return None
    authors = []
    for author in entry.findall("a:author", _ATOM):
        name = (author.findtext("a:name", "", _ATOM) or "").strip()
        given, _, family = name.rpartition(" ")
        authors.append({"given": given, "family": family})
    published = entry.findtext("a:published", "", _ATOM)
    return Record(
        title=re.sub(r"\s+", " ", entry.findtext("a:title", "", _ATOM)).strip(),
        authors=authors,
        year=int(published[:4]) if published[:4].isdigit() else None,
        venue="arXiv",
        type="preprint",
        doi=f"10.48550/arxiv.{arxiv_id}",
        abstract=re.sub(r"\s+", " ", entry.findtext("a:summary", "", _ATOM)).strip() or None,
        source="arxiv",
    )


# --- One entry point ----------------------------------------------------------------

def by_ids(ids: dict[str, str]) -> tuple[Record | None, str | None]:
    """Try each identifier in order of reliability. Returns (record, which id worked)."""
    if doi := ids.get("doi"):
        if record := by_doi(doi):
            return record, "doi"
    if nber := ids.get("nber"):
        # NBER registers every working paper under this DOI pattern.
        if record := by_doi(f"10.3386/w{nber}"):
            return record, "nber"
    if arxiv := ids.get("arxiv"):
        if record := by_arxiv(arxiv):
            return record, "arxiv"
    if jstor := ids.get("jstor"):
        # Older JSTOR-hosted articles are registered as 10.2307/<stable id>.
        if record := by_doi(f"10.2307/{jstor}"):
            return record, "jstor"
    return None, None
