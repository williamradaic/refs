"""Ask the registries: given an identifier, return the paper's metadata.

This is where bibliographic *facts* come from. Crossref holds DOIs for journal
articles and NBER working papers; arXiv has its own service. Responses are
cached on this machine, so each identifier is fetched once.

Two ways in:
  by_ids()      an identifier printed on the PDF (exact)
  best_match()  a title/author/year read off the PDF, searched and then CHECKED:
                a search result is only accepted if its title and authors agree
                with what is printed. The check is plain arithmetic, not a model.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
import unicodedata
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from pathlib import Path

import httpx
from rapidfuzz import fuzz

CACHE_DIR = Path.home() / "Library" / "Caches" / "refs" / "http"
# A generic client name. Registries answer faster when given a contact email,
# but none is sent unless you add one yourself (a later config option).
USER_AGENT = "refs/0.1 (personal reference manager)"
MAX_ATTEMPTS = 6
# Minimum seconds between two requests to the same host. Crossref allows
# anonymous clients one request per second and says so in its response
# headers; the pause for each host is updated from those headers as we go.
_pause: dict[str, float] = {"api.crossref.org": 1.05}
_DEFAULT_PAUSE = 0.15
_next_allowed: dict[str, float] = {}
# Several files are processed at once, but registry requests go out one at a
# time: Crossref answers "429 Too Many Requests" to bursts.
_request_lock = threading.Lock()


class Unavailable(Exception):
    """A registry cannot be used for now (its usage quota is exhausted)."""


# When a registry says "come back in N seconds" with a large N, that is a used-up
# quota, not a burst limit. Remember it on disk, so every later command skips
# that registry at once instead of waiting on it.
_BLOCK_FILE = CACHE_DIR.parent / "registry-blocks.json"
_QUOTA_THRESHOLD = 120          # a retry-after longer than this many seconds = quota


def blocked_for(host: str) -> float:
    """Seconds until `host` may be used again (0 if it is usable now)."""
    try:
        until = json.loads(_BLOCK_FILE.read_text()).get(host, 0)
    except (OSError, ValueError):
        return 0.0
    return max(0.0, until - time.time())


def _block(host: str, seconds: float) -> None:
    try:
        blocks = json.loads(_BLOCK_FILE.read_text())
    except (OSError, ValueError):
        blocks = {}
    blocks[host] = time.time() + seconds
    _BLOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    _BLOCK_FILE.write_text(json.dumps(blocks))


_email_cache: list[str] = []


def _contact_email() -> str:
    """Read the optional contact email once per run."""
    if not _email_cache:
        from .config import contact_email
        _email_cache.append(contact_email())
    return _email_cache[0]


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
    online_date_only: bool = False  # registry has no print date; year may be "online first"
    authors_source: str = ""        # set when the authors came from a second registry
    title_source: str = ""          # "pdf" when the registry title was unusable (see check)

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

    host = httpx.URL(url).host
    if blocked_for(host):
        raise Unavailable(host)
    email = _contact_email()
    agent = f"{USER_AGENT[:-1]}; mailto:{email})" if email else USER_AGENT
    with _request_lock:
        for attempt in range(MAX_ATTEMPTS):
            # Wait until this host may be asked again.
            time.sleep(max(0.0, _next_allowed.get(host, 0.0) - time.monotonic()))
            response = httpx.get(url, params=params, headers={"User-Agent": agent},
                                 timeout=30, follow_redirects=True)
            # "x-rate-limit-limit: 1" with "x-rate-limit-interval: 1s" means one
            # request per second: space requests accordingly, plus a margin.
            limit = response.headers.get("x-rate-limit-limit", "")
            interval = response.headers.get("x-rate-limit-interval", "").rstrip("s")
            if limit.isdigit() and interval.replace(".", "", 1).isdigit() and int(limit) > 0:
                _pause[host] = float(interval) / int(limit) * 1.05
            _next_allowed[host] = time.monotonic() + _pause.get(host, _DEFAULT_PAUSE)
            # Rate limit or temporary outage: wait (as long as the server asks,
            # otherwise 2, 4, 8, ... seconds) and try again.
            asked = response.headers.get("retry-after", "")
            if response.status_code == 429 and asked.isdigit() and int(asked) > _QUOTA_THRESHOLD:
                _block(host, int(asked))               # quota used up: do not wait for it
                raise Unavailable(host)
            if response.status_code in {429, 500, 502, 503, 504} and attempt < MAX_ATTEMPTS - 1:
                time.sleep(min(float(asked) if asked.isdigit() else 2 ** (attempt + 1), 60))
                continue
            break
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    if response.status_code == 404:
        cached.write_bytes(b"404")     # remember misses too
        return None
    response.raise_for_status()         # other errors are not cached
    cached.write_bytes(response.content)
    return response


# --- Crossref ---------------------------------------------------------------------

def _strip_tags(text: str) -> str:
    """Crossref titles and abstracts can contain markup such as <i> or <jats:p>."""
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text)).strip()


def _clean_title(title: str) -> str:
    """Remove publisher debris: a footnote star at the end, "Chapter 16" at the start."""
    title = re.sub(r"\s*[*†‡]+$", "", title)
    return re.sub(r"^Chapter\s+\d+[.:]?\s+", "", title, flags=re.IGNORECASE).strip()


def _from_crossref(msg: dict) -> Record:
    # Several date fields exist; the earliest publication date is the citable year.
    year = None
    for key in ("published-print", "published", "issued", "published-online"):
        parts = (msg.get(key) or {}).get("date-parts") or [[None]]
        if parts[0] and parts[0][0]:
            year = int(parts[0][0])
            break
    title = _clean_title(_strip_tags(" ".join(msg.get("title") or [])))
    if msg.get("subtitle"):
        title += ": " + _strip_tags(" ".join(msg["subtitle"]))
    return Record(
        online_date_only=not (msg.get("published-print") or {}).get("date-parts"),
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
    try:
        response = _get(f"https://api.openalex.org/works/doi:{doi}")
    except Unavailable:
        return []
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


# --- Searching by title ---------------------------------------------------------------

_CROSSREF_SKIP_TYPES = {"component", "peer-review", "journal-issue", "journal", "dataset", "grant"}
_REPLY_RE = re.compile(r"^(comment|reply|erratum|corrigendum|correction|a comment|rejoinder)\b", re.I)
# Kinds of record that are a finished publication (preferred over a working paper).
PUBLISHED_TYPES = {"journal-article", "book", "book-chapter", "monograph", "edited-book",
                   "reference-book", "proceedings-article"}


_PREPRINT_DOI_PREFIXES = ("10.2139/ssrn", "10.3386/", "10.48550/", "10.1920/wp", "10.31235/", "10.21203/")
_PREPRINT_VENUE_RE = re.compile(r"ssrn|working paper|discussion paper|arxiv|repec|preprint", re.I)


def is_published(record: Record) -> bool:
    """True for a finished publication, False for a working paper or preprint.

    Crossref lists SSRN postings as "journal-article" in the "SSRN Electronic
    Journal", so the type alone cannot be trusted.
    """
    if (record.doi or "").startswith(_PREPRINT_DOI_PREFIXES):
        return False
    if record.venue and _PREPRINT_VENUE_RE.search(record.venue):
        return False
    return record.type in PUBLISHED_TYPES


def _from_openalex(work: dict) -> Record:
    source = ((work.get("primary_location") or {}).get("source") or {})
    kind = work.get("type")
    if kind == "article" and source.get("type") == "journal":
        kind = "journal-article"          # use Crossref's vocabulary throughout
    authors = []
    for item in work.get("authorships") or []:
        name = ((item.get("author") or {}).get("display_name") or "").strip()
        if name:
            given, _, family = name.rpartition(" ")
            authors.append({"given": given, "family": family})
    biblio = work.get("biblio") or {}
    pages = "-".join(p for p in [biblio.get("first_page"), biblio.get("last_page")] if p) or None
    return Record(
        title=_strip_tags(work.get("title") or work.get("display_name") or ""),
        authors=authors, year=work.get("publication_year"),
        venue=source.get("display_name"), type=kind,
        doi=(work.get("doi") or "").lower().removeprefix("https://doi.org/") or None,
        volume=biblio.get("volume"), issue=biblio.get("issue"), pages=pages,
        source="openalex",
    )


def search_crossref(title: str, surnames: list[str], year: int | None,
                    venue: str | None = None) -> list[Record]:
    # The venue helps Crossref rank the original article above later reprints.
    query = " ".join([title, *surnames[:3], str(year or ""), venue or ""]).strip()
    response = _get("https://api.crossref.org/works", {"query.bibliographic": query, "rows": 12})
    if response is None:
        return []
    items = response.json()["message"].get("items") or []
    return [_from_crossref(i) for i in items
            if i.get("type") not in _CROSSREF_SKIP_TYPES and i.get("title")]


def search_openalex(title: str) -> list[Record]:
    # OpenAlex treats commas and some punctuation as query syntax; send plain words.
    plain = re.sub(r"[^\w\s]", " ", title)
    response = _get("https://api.openalex.org/works", {"search": plain, "per-page": 8})
    if response is None:
        return []
    return [_from_openalex(w) for w in response.json().get("results") or [] if w.get("title")]


# --- Checking a candidate against what is printed -------------------------------------

def _plain(text: str) -> str:
    """Lowercase letters and digits only, accents removed: for comparing titles."""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def title_similarity(a: str, b: str) -> float:
    """0 to 100. Also compares main titles alone, since subtitles are often dropped."""
    full = fuzz.ratio(_plain(a), _plain(b))
    main_a, main_b = _plain(re.split(r"[:?]", a)[0]), _plain(re.split(r"[:?]", b)[0])
    if min(len(main_a), len(main_b)) >= 20:        # short main titles are not distinctive
        full = max(full, fuzz.ratio(main_a, main_b) - 3)   # small penalty for ignoring subtitle
    return full


def surname_overlap(printed: list[str], registry: list[str]) -> float:
    """Share of printed surnames found in the registry record (fuzzy, accent-blind)."""
    printed_plain = [_plain(s) for s in printed if _plain(s)]
    registry_plain = [_plain(s) for s in registry if _plain(s)]
    if not printed_plain or not registry_plain:
        return 0.0
    hits = sum(
        1 for p in printed_plain
        # partial_ratio lets "Scott Morton" match "Morton", and tolerates OCR slips.
        if any(fuzz.ratio(p, r) >= 85 or (min(len(p), len(r)) >= 4 and fuzz.partial_ratio(p, r) >= 92)
               for r in registry_plain)
    )
    return hits / len(printed_plain)


@dataclass
class Match:
    """A registry record judged against the printed title, authors and year."""

    record: Record
    title_similarity: float
    author_overlap: float
    year_gap: int | None
    accepted: bool


def check(record: Record, title: str, surnames: list[str], year: int | None) -> Match:
    if not record.authors and record.doi:          # Crossref gap: borrow from OpenAlex
        record.authors = _openalex_authors(record.doi)
        record.authors_source = "openalex" if record.authors else ""
    sim = title_similarity(title, record.title)
    # Some registry titles have the first footnote glued on ("A model of investor
    # sentiment1We are grateful to..."). If the registry title begins with the
    # printed title and then runs on, it is the same paper: keep the printed title.
    printed, registered = _plain(title), _plain(record.title)
    if len(printed) >= 15 and registered.startswith(printed) and len(registered) > 1.3 * len(printed):
        record.title, record.title_source, sim = title, "pdf", 97.0
    overlap = surname_overlap(surnames, record.surnames)
    gap = abs(record.year - year) if (record.year and year) else None
    accepted = (
        # Normal case: title agrees and at least half the printed authors are there.
        (sim >= 90 and overlap >= 0.5)
        # No authors legible on the PDF: demand a near-exact title and the same year.
        or (not surnames and sim >= 96 and gap is not None and gap <= 1)
    )
    # A "Reply" or "Comment" shares its title with the paper it answers.
    if _REPLY_RE.match(record.title) and not _REPLY_RE.match(title):
        accepted = False
    return Match(record, sim, overlap, gap, accepted)


def best_match(title: str, surnames: list[str], year: int | None, *,
               venue: str | None = None, is_working_paper: bool = False,
               ) -> tuple[Match | None, list[str]]:
    """Search the registries and return the best candidate that passes the check,
    plus the names of any registry that had to be skipped (quota used up).

    Passing the check (title and authors agree) establishes that a candidate is
    the same *work*. This function then picks the right *version* of it:

      * the PDF is a published article  -> a published record from the same year
        (give or take one). A match seven years later is a reprint in a book.
      * the PDF is a working paper      -> the published version if one exists
        (it must not predate the draft), otherwise the working-paper record.

    OpenAlex is asked as well whenever Crossref offers no ideal candidate.
    """
    def ideal(m: Match) -> bool:
        if not is_published(m.record):
            return False
        if m.year_gap is None:
            return True
        if is_working_paper:
            return m.record.year >= year - 1
        return m.year_gap <= 1

    def rank(m: Match) -> tuple:
        return (not ideal(m), not is_published(m.record) if is_working_paper else is_published(m.record),
                -m.title_similarity, m.year_gap if m.year_gap is not None else 99)

    accepted: list[Match] = []
    skipped: list[str] = []
    for name, candidates in (("crossref", lambda: search_crossref(title, surnames, year, venue)),
                             ("openalex", lambda: search_openalex(title))):
        try:
            found = candidates()
        except Unavailable:
            skipped.append(name)
            continue
        accepted += [m for m in (check(r, title, surnames, year) for r in found) if m.accepted]
        if any(ideal(m) for m in accepted):
            break
    if not accepted:
        return None, skipped
    best = sorted(accepted, key=rank)[0]
    # A published record far from the printed year, for a PDF that is itself a
    # published article, is a different edition or a reprint: do not use it.
    if is_published(best.record) and not ideal(best):
        return None, skipped
    return best, skipped
