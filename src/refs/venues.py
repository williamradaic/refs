"""Venues: what to show in the journal column, and short names for journals.

Registries sometimes name a *repository* where a journal should be (RePEc,
SSRN, a university archive, a publisher's e-book platform). Those are places a
file is hosted, not places a work was published, so they are never shown as
the venue.
"""

from __future__ import annotations

from functools import lru_cache
import html
import json
import re
from pathlib import Path

_REPOSITORY_RE = re.compile(
    r"repec|research papers in economics|ssrn|arxiv|research archive|ebooks|zenodo|"
    r"munich personal|econstor|doaj|figshare|\bosf\b|\bhal\b|scholarworks|digital commons",
    re.IGNORECASE)

# Short names, in the forms used in ~/Zotero/journals_abbrev.json where that
# file has an opinion. Keys are normalised (see _norm).
_ABBREVIATIONS = {
    "american economic review": "AER", "quarterly journal of economics": "QJE",
    "journal of political economy": "JPE", "econometrica": "ECTA",
    "review of economic studies": "REStud", "review of economics and statistics": "REStat",
    "journal of economic literature": "JEL", "journal of economic perspectives": "JEP",
    "journal of public economics": "JPubE", "journal of economic theory": "JET",
    "theoretical economics": "TE", "quantitative economics": "QE",
    "journal of the european economic association": "JEEA", "economic journal": "EJ",
    "journal of economic behavior and organization": "JEBO",
    "games and economic behavior": "GEB", "journal of development economics": "JDE",
    "american economic journal applied economics": "AEJ-Applied",
    "american economic journal economic policy": "AEJ-Policy",
    "american economic journal macroeconomics": "AEJ-Macro",
    "american economic journal microeconomics": "AEJ-Micro",
    "american economic review insights": "AER-Insights",
    "aea papers and proceedings": "AEA P&P",
    "journal of political economy microeconomics": "JPE-Micro",
    "journal of political economy macroeconomics": "JPE-Macro",
    "rand journal of economics": "RAND", "journal of industrial economics": "JIndE",
    "international journal of industrial organization": "IJIO",
    "journal of finance": "JF", "journal of financial economics": "JFE",
    "review of financial studies": "RFS", "journal of econometrics": "JoE",
    "journal of monetary economics": "JME", "journal of labor economics": "JOLE",
    "journal of human resources": "JHR", "journal of health economics": "JHE",
    "journal of international economics": "JIE", "international economic review": "IER",
    "journal of economic growth": "JEG", "journal of risk and uncertainty": "JRU",
    "journal of urban economics": "JUE", "journal of law and economics": "JLE",
    "journal of economics and management strategy": "JEMS",
    "annual review of economics": "ARE", "brookings papers on economic activity": "BPEA",
    "journal of the american statistical association": "JASA",
    "management science": "MS", "marketing science": "MktSci",
    "quantitative marketing and economics": "QME", "experimental economics": "ExpEcon",
    "american journal of political science": "AJPS",
    "american political science review": "APSR", "journal of politics": "JOP",
    "proceedings of the national academy of sciences": "PNAS",
    "psychological review": "PsychRev", "psychological science": "PsychSci",
    "journal of personality and social psychology": "JPSP",
    "nature human behaviour": "NHB", "canadian journal of economics": "CJE",
    "economics letters": "EL", "journal of economic education": "JEE",
    "national bureau of economic research": "NBER", "nber working paper": "NBER",
    "nber working paper series": "NBER",
}


@lru_cache(maxsize=1024)
def _norm(name: str) -> str:
    name = html.unescape(name).lower().replace("&", " and ")
    name = re.sub(r"[^a-z0-9 ]+", " ", name)
    return re.sub(r"\s+", " ", re.sub(r"^the ", "", name.strip())).strip()


def _load_user_abbreviations() -> dict[str, str]:
    """Your own list from Zotero, if present, wins over the built-in one."""
    path = Path.home() / "Zotero" / "journals_abbrev.json"
    try:
        return {_norm(k): v for k, v in json.loads(path.read_text()).items()}
    except (OSError, ValueError):
        return {}


def is_repository(venue: str | None) -> bool:
    return bool(venue and _REPOSITORY_RE.search(venue))


def clean(venue: str | None) -> str | None:
    """A venue fit to display and cite: entities decoded, repositories dropped."""
    if not venue:
        return None
    venue = re.sub(r"&amp(?!;)", "&", html.unescape(venue)).strip()   # "Philosophy &amp Public"
    return None if is_repository(venue) else venue


def abbreviation(venue: str | None) -> str | None:
    """"The Quarterly Journal of Economics" -> "QJE"; None for unknown journals."""
    if not venue:
        return None
    return _ABBREVIATIONS.get(_norm(venue))


def label(meta: dict) -> str:
    """What the venue column shows: the journal for articles, and for everything
    else what kind of thing it is."""
    venue, kind = meta.get("venue"), meta.get("kind")
    if kind == "book":
        return "book"
    if kind == "chapter":
        return f"in {venue}" if venue else "book chapter"
    if (meta.get("doi") or "").startswith("10.3386/"):
        return "NBER working paper"
    record_type = meta.get("type") or ""
    if venue and record_type not in {"report", "posted-content", "preprint"}:
        return venue
    if meta.get("is_working_paper") or record_type in {"report", "posted-content", "preprint"}:
        return venue if venue and "working paper" in venue.lower() else "working paper"
    return venue or "unpublished"
