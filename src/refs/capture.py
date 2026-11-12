"""Quick add: turn a link or identifier into a PDF in the dump folder.

    refs add https://www.nber.org/papers/w32515
    refs add 2301.01234
    refs add 10.1257/aer.20220276
    refs add ~/Downloads/some-paper.pdf

Only openly available PDFs can be fetched: the app has no library login. When
no open copy exists, the paper is still saved as a work without a file, and
the PDF attaches itself when you later drop it into the dump.
"""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from . import extract, resolve

HEADERS = {"User-Agent": resolve.USER_AGENT}


@dataclass
class Target:
    """What a quick-add input refers to."""

    ids: dict[str, str] = field(default_factory=dict)
    pdf_url: str | None = None
    page_url: str | None = None


def parse(thing: str) -> Target:
    """Recognise a DOI, arXiv id, NBER number or URL in what the user typed."""
    thing = thing.strip()
    target = Target()
    is_url = thing.lower().startswith(("http://", "https://"))

    if m := re.search(r"arxiv\.org/(?:abs|pdf)/(\d{4}\.\d{4,5})", thing, re.I) or \
            re.fullmatch(r"(?:arxiv:)?(\d{4}\.\d{4,5})(?:v\d+)?", thing, re.I):
        target.ids["arxiv"] = m.group(1)
    elif m := re.search(r"nber\.org/papers/w(\d{3,5})", thing, re.I) or \
            re.fullmatch(r"w(\d{4,5})", thing, re.I):
        target.ids["nber"] = m.group(1)
    elif m := extract.DOI_RE.search(thing):
        target.ids["doi"] = extract.clean_doi(m.group(1))

    if is_url:
        if re.search(r"\.pdf($|[?#])", thing, re.I):
            target.pdf_url = thing
        else:
            target.page_url = thing
    return target


def discover(target: Target) -> Target:
    """Find where an open PDF can be downloaded, filling in `pdf_url`."""
    if target.pdf_url:
        return target
    if arxiv := target.ids.get("arxiv"):
        target.pdf_url = f"https://arxiv.org/pdf/{arxiv}"
        return target
    if nber := target.ids.get("nber"):
        target.pdf_url = f"https://www.nber.org/system/files/working_papers/w{nber}/w{nber}.pdf"
        return target

    # A web page: publishers and repositories describe the paper in <meta> tags
    # (the same ones Google Scholar reads).
    if target.page_url:
        try:
            html = httpx.get(target.page_url, headers=HEADERS, timeout=30, follow_redirects=True).text
        except httpx.HTTPError:
            html = ""
        def meta(name: str) -> str | None:
            m = re.search(rf'<meta[^>]+name=["\']{name}["\'][^>]+content=["\']([^"\']+)', html, re.I)
            return m.group(1) if m else None
        target.pdf_url = meta("citation_pdf_url")
        if doi := meta("citation_doi"):
            target.ids.setdefault("doi", extract.clean_doi(doi))
        if arxiv := meta("citation_arxiv_id"):
            target.ids.setdefault("arxiv", arxiv)

    # A DOI: ask OpenAlex whether an open-access copy exists anywhere.
    if not target.pdf_url and (doi := target.ids.get("doi")):
        try:
            response = resolve._get(f"https://api.openalex.org/works/doi:{doi}")
        except (httpx.HTTPError, resolve.Unavailable):
            response = None
        if response is not None:
            work = response.json()
            target.pdf_url = ((work.get("best_oa_location") or {}).get("pdf_url")
                              or (work.get("open_access") or {}).get("oa_url"))
    return target


def download(url: str, folder: Path) -> Path | None:
    """Download a PDF into `folder`. None unless the response really is a PDF."""
    try:
        response = httpx.get(url, headers=HEADERS, timeout=60, follow_redirects=True)
    except httpx.HTTPError:
        return None
    # Paywalls and login pages answer with HTML: every real PDF starts with "%PDF".
    if response.status_code != 200 or not response.content.startswith(b"%PDF"):
        return None
    # A temporary name; the file is renamed properly once it has been identified.
    path = folder / f"added-{secrets.token_hex(4)}.pdf"
    path.write_bytes(response.content)
    return path
