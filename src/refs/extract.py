"""Read a PDF: its first pages of text and any identifier printed on them.

Nothing here guesses. An identifier (DOI, arXiv, NBER, SSRN, JSTOR) is either
printed on the opening pages or it is not; when it is, a registry can return
the paper's metadata exactly (see resolve.py).
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

import pymupdf

# MuPDF prints warnings about slightly malformed PDFs straight to the terminal.
# They are harmless here, so keep the output clean.
pymupdf.TOOLS.mupdf_display_errors(False)

# How much of the document is read. The model gets three pages (title, abstract,
# start of the introduction). Identifiers are taken from the first two only:
# further in, citations start to carry *other* papers' DOIs.
FIRST_PAGES = 3
ID_PAGES = 2
MAX_CHARS = 10000

# A DOI: "10.", a registrant number, a slash, then anything up to whitespace.
DOI_RE = re.compile(r"\b(10\.\d{4,9}/[^\s\"<>{}|\\^`]+)", re.IGNORECASE)
ARXIV_RE = re.compile(r"arXiv:\s?(\d{4}\.\d{4,5})", re.IGNORECASE)
NBER_URL_RE = re.compile(r"nber\.org/papers/w(\d{3,5})", re.IGNORECASE)
NBER_WP_RE = re.compile(r"Working Paper\s+(?:No\.?\s*)?(\d{3,5})\b", re.IGNORECASE)
SSRN_RE = re.compile(r"ssrn\.com/abstract(?:_id)?=(\d{5,9})|abstract_id=(\d{5,9})", re.IGNORECASE)
JSTOR_RE = re.compile(r"jstor\.org/stable/(\d{4,10})", re.IGNORECASE)


@dataclass
class PdfInfo:
    """What was read from one PDF."""

    pages: int = 0
    text: str = ""                    # opening pages, whitespace collapsed
    has_text: bool = False            # False for scans without a text layer
    meta_title: str | None = None     # embedded metadata: often junk, never trusted alone
    meta_author: str | None = None
    ids: dict[str, str] = field(default_factory=dict)
    fingerprint: str | None = None    # stable across annotation: hash of the text
    error: str | None = None


def clean_doi(raw: str) -> str:
    """Trim what the pattern over-captures: trailing punctuation, stray brackets."""
    doi = raw.rstrip(".,;:)]}>'\"")
    # "10.1257/aer.20220276)" inside brackets: drop an unmatched closing bracket.
    while doi.endswith(")") and doi.count("(") < doi.count(")"):
        doi = doi[:-1]
    return doi.lower()


def find_ids(text: str) -> dict[str, str]:
    """Identifiers printed in `text` (the opening pages of a PDF)."""
    ids: dict[str, str] = {}
    if m := DOI_RE.search(text):
        ids["doi"] = clean_doi(m.group(1))
    if m := ARXIV_RE.search(text):
        ids["arxiv"] = m.group(1)
    if m := NBER_URL_RE.search(text):
        ids["nber"] = m.group(1)
    elif (m := NBER_WP_RE.search(text)) and re.search(r"NBER|National Bureau of Economic", text):
        ids["nber"] = m.group(1)
    if m := SSRN_RE.search(text):
        ids["ssrn"] = m.group(1) or m.group(2)
    if m := JSTOR_RE.search(text):
        ids["jstor"] = m.group(1)
    return ids


def fingerprint(text: str) -> str | None:
    """A hash of the opening text with everything but letters and digits removed.

    Highlighting or annotating a PDF changes its bytes (so its SHA-256) but not
    its text, so this still matches the original.
    """
    letters = re.sub(r"[^a-z0-9]", "", text.lower())[:3000]
    if len(letters) < 200:           # too little text to be distinctive
        return None
    return hashlib.sha1(letters.encode()).hexdigest()


def clean_text(raw: str) -> str:
    """Normalise text taken from a PDF page.

    NFKC turns typeset ligatures back into letters ("ﬁ" -> "fi"); without it a
    DOI such as 10.1016/j.jfineco... is read with a character no registry knows.
    """
    text = unicodedata.normalize("NFKC", raw)
    return re.sub(r"\s+", " ", text).strip()[:MAX_CHARS]


def read_pdf(path: Path) -> PdfInfo:
    """Open a PDF and collect the opening text, metadata and identifiers."""
    info = PdfInfo()
    try:
        with pymupdf.open(path) as doc:
            info.pages = doc.page_count
            meta = doc.metadata or {}
            info.meta_title = (meta.get("title") or "").strip() or None
            info.meta_author = (meta.get("author") or "").strip() or None
            chunks = [doc[i].get_text() for i in range(min(FIRST_PAGES, doc.page_count))]
    except Exception as error:        # damaged or password-protected file
        info.error = f"{type(error).__name__}: {error}"
        return info

    info.text = clean_text(" ".join(chunks))
    info.has_text = len(info.text) >= 200
    info.ids = find_ids(clean_text(" ".join(chunks[:ID_PAGES])))
    info.fingerprint = fingerprint(info.text)
    return info


def first_page_png(path: Path, dpi: int = 110) -> bytes:
    """Render page 1 as a PNG image, for scans that have no text layer."""
    with pymupdf.open(path) as doc:
        return doc[0].get_pixmap(dpi=dpi).tobytes("png")
