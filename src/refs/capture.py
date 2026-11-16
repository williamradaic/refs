"""Quick add: turn a link or identifier into a PDF in the dump folder.

    refs add https://www.nber.org/papers/w32515
    refs add 2301.01234
    refs add 10.1257/aer.20220276
    refs add ~/Downloads/some-paper.pdf

The command line can fetch only PDFs it may download itself (open access, or
what this Mac's network address gives it). The "+ refs" bookmarklet does
better: it reads the page and fetches the PDF in your browser, signed in as
you, and hands both over (the /capture page). When no PDF can be had, the
paper is still saved as a work without a file, and the PDF attaches itself
when you later drop it into the dump.
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
    pdf_url: str | None = None          # the first place to try; see `pdf_urls`
    page_url: str | None = None
    pdf_urls: list[str] = field(default_factory=list)   # every candidate, best first

    def offer(self, url: str | None) -> None:
        """Add a place a PDF may be downloaded from (each once, in order)."""
        if url and url not in self.pdf_urls:
            self.pdf_urls.append(url)
            self.pdf_url = self.pdf_urls[0]


# NBER's own address forms: the paper page, its PDF link, and the DOI NBER
# registers every working paper under (10.3386/w32515).
NBER_RE = re.compile(r"nber\.org/(?:papers|system/files/working_papers)/w(\d{3,5})"
                     r"|\b10\.3386/w(\d{3,5})\b", re.I)


def parse(thing: str) -> Target:
    """Recognise a DOI, arXiv id, NBER number or URL in what the user typed."""
    thing = thing.strip()
    target = Target()
    is_url = thing.lower().startswith(("http://", "https://"))

    if m := re.search(r"arxiv\.org/(?:abs|pdf)/(\d{4}\.\d{4,5})", thing, re.I) or \
            re.fullmatch(r"(?:arxiv:)?(\d{4}\.\d{4,5})(?:v\d+)?", thing, re.I):
        target.ids["arxiv"] = m.group(1)
    elif m := NBER_RE.search(thing) or re.fullmatch(r"w(\d{4,5})", thing, re.I):
        target.ids["nber"] = next(g for g in m.groups() if g)
    elif m := extract.DOI_RE.search(thing):
        target.ids["doi"] = extract.clean_doi(m.group(1))

    if is_url:
        if re.search(r"\.pdf($|[?#])", thing, re.I):
            target.offer(thing)
        else:
            target.page_url = thing
    return target


def hinted(target: Target, *, doi: str | None = None, pdf_url: str | None = None) -> Target:
    """Add what the browser read off the page (the bookmarklet): the page's
    own DOI beats one guessed from its address, and its PDF link comes first."""
    if doi and (m := extract.DOI_RE.search(doi)):
        found = extract.clean_doi(m.group(1))
        if nber := NBER_RE.search(found):
            target.ids.setdefault("nber", next(g for g in nber.groups() if g))
        target.ids["doi"] = found
    if pdf_url and pdf_url.lower().startswith(("http://", "https://")):
        if pdf_url in target.pdf_urls:
            target.pdf_urls.remove(pdf_url)
        target.pdf_urls.insert(0, pdf_url)
        target.pdf_url = pdf_url
    return target


def page_meta(html: str) -> dict[str, list[str]]:
    """The <meta name=… content=…> tags of a page (either attribute order),
    lower-case name → every value. Publishers describe a paper in them (the
    citation_* tags Google Scholar reads)."""
    found: dict[str, list[str]] = {}
    for tag in re.findall(r"<meta\b[^>]*>", html, re.I):
        attrs = dict((k.lower(), v) for k, _, v in
                     re.findall(r'([\w:.-]+)\s*=\s*(["\'])(.*?)\2', tag, re.S))
        name, content = attrs.get("name") or attrs.get("property"), attrs.get("content")
        if name and content:
            found.setdefault(name.lower(), []).append(content.strip())
    return found


def discover(target: Target, *, fetch_page: bool = True) -> Target:
    """List where a PDF can be downloaded (`pdf_urls`, best first). An
    open-access copy elsewhere is looked up only if these fail (open_copies).

    `fetch_page=False` when the browser already read the page (the
    bookmarklet): publishers such as OUP refuse programs like this one, and
    the page would only say what the browser already passed on.
    """
    if arxiv := target.ids.get("arxiv"):
        target.offer(f"https://arxiv.org/pdf/{arxiv}")
    if nber := target.ids.get("nber"):
        target.offer(f"https://www.nber.org/system/files/working_papers/w{nber}/w{nber}.pdf")

    # A web page: read its <meta> tags.
    if target.page_url and fetch_page and not target.pdf_urls:
        try:
            html = httpx.get(target.page_url, headers=HEADERS, timeout=15, follow_redirects=True).text
        except httpx.HTTPError:
            html = ""
        meta = page_meta(html)
        first = lambda name: (meta.get(name) or [None])[0]   # noqa: E731
        target.offer(first("citation_pdf_url"))
        if doi := first("citation_doi"):
            hinted(target, doi=doi)
        if arxiv := first("citation_arxiv_id"):
            target.ids.setdefault("arxiv", arxiv)

    return target


def open_copies(doi: str) -> list[str]:
    """Open-access copies of a DOI that OpenAlex knows (an author's site, a
    repository): the fallback when the publisher's own link is paywalled."""
    try:
        response = resolve._get(f"https://api.openalex.org/works/doi:{doi}")
    except (httpx.HTTPError, resolve.Unavailable):
        return []
    if response is None:
        return []
    work = response.json()
    urls = [(work.get("best_oa_location") or {}).get("pdf_url"),
            (work.get("open_access") or {}).get("oa_url")]
    return [u for u in dict.fromkeys(urls) if u]


def download(url: str, folder: Path) -> Path | None:
    """Download a PDF into `folder`. None unless the response really is a PDF."""
    try:
        response = httpx.get(url, headers=HEADERS, timeout=60, follow_redirects=True)
    except httpx.HTTPError:
        return None
    # Paywalls and login pages answer with HTML: every real PDF starts with "%PDF".
    if response.status_code != 200 or not response.content.startswith(b"%PDF"):
        return None
    return save(response.content, folder)


def download_first(urls: list[str], folder: Path) -> Path | None:
    """The first of `urls` that gives a real PDF (the next one is tried when
    a link turns out to be a paywall or login page)."""
    for url in urls:
        if path := download(url, folder):
            return path
    return None


def save(content: bytes, folder: Path) -> Path:
    """Write a PDF into `folder` under a temporary name; the file is renamed
    properly once it has been identified (ingest.py renames "added-" files)."""
    path = folder / f"added-{secrets.token_hex(4)}.pdf"
    path.write_bytes(content)
    return path


# --- the bookmarklet's key --------------------------------------------------------------------

def token(cfg) -> str:
    """The secret built into this Mac's "+ refs" bookmarklet. Only a page that
    knows it can make refs add a paper without you pressing Enter (another
    website could open the refs window, but cannot read your bookmarks).
    Kept next to the index, outside the synced library: one per Mac."""
    path = cfg.index_db.parent / "bookmarklet-token"
    try:
        value = path.read_text().strip()
    except OSError:
        value = ""
    if len(value) < 20:
        path.parent.mkdir(parents=True, exist_ok=True)
        value = secrets.token_urlsafe(24)
        path.write_text(value + "\n")
        path.chmod(0o600)
    return value
