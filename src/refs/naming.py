"""File names: read what a name already says, and build the canonical name.

Two directions:
  parse_filename("berry-levinsohn-pakes-1995-automobile-prices.pdf")
      -> a *guess* at authors, year and title. Only a guess: later phases
         replace it with registry-verified metadata.
  canonical_name(["Berry", "Levinsohn", "Pakes"], 1995, "Automobile Prices in ...")
      -> "berry-levinsohn-pakes-1995-automobile-prices-in-market-equilibrium.pdf"
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from urllib.parse import unquote

YEAR_RE = re.compile(r"^(1[6-9]\d\d|20\d\d)$")
# Words that are not surnames even when capitalised next to a year.
NOT_AUTHORS = {"et", "al", "and", "aer", "qje", "jpe", "restud", "res", "ecma", "jep", "jel", "nber", "wp"}
STOPWORDS = {"a", "an", "the", "of", "in", "on", "for", "and", "to", "with", "by", "from", "at", "as", "or"}
# Letters that Unicode does not decompose into "base letter + accent".
SPECIAL_LETTERS = str.maketrans({"ø": "o", "Ø": "O", "ł": "l", "Ł": "L", "đ": "d", "ß": "ss", "æ": "ae", "œ": "oe"})


@dataclass
class NameGuess:
    """What a file name suggests. Every field may be empty."""

    authors: list[str] = field(default_factory=list)   # surnames, as written
    year: int | None = None
    title: str | None = None
    ids: dict[str, str] = field(default_factory=dict)  # e.g. {"nber": "32515"}
    style: str = "none"   # which pattern matched: convention, jstor, loose, id, none

    @property
    def looks_like_paper(self) -> bool:
        """True when the name carries an identifier, or authors plus a year."""
        return bool(self.ids) or (bool(self.authors) and self.year is not None)


# --- Parsing ----------------------------------------------------------------

def _split_camel(text: str) -> str:
    """"CostsBenefitsOwnership" -> "Costs Benefits Ownership"."""
    return re.sub(r"(?<=[a-z])(?=[A-Z])", " ", text)


def parse_filename(filename: str) -> NameGuess:
    """Guess authors, year and title from a PDF's file name."""
    stem = unquote(filename)                      # "k%C5%91szegi" -> "kőszegi"
    stem = re.sub(r"\.pdf$", "", stem, flags=re.IGNORECASE).strip()

    # 1. Bare identifiers, the way sites name their downloads.
    if m := re.fullmatch(r"w(\d{4,5})", stem, flags=re.IGNORECASE):
        return NameGuess(ids={"nber": m.group(1)}, style="id")
    if m := re.fullmatch(r"ssrn[-_ ]?(\d{5,9})", stem, flags=re.IGNORECASE):
        return NameGuess(ids={"ssrn": m.group(1)}, style="id")
    if m := re.fullmatch(r"(\d{4}\.\d{4,5})(v\d+)?", stem):
        return NameGuess(ids={"arxiv": m.group(1)}, style="id")

    # 2. The house convention: surname(s)-year-short-title, lowercase, hyphens.
    if stem == stem.lower() and "-" in stem and " " not in stem:
        tokens = stem.split("-")
        for i, token in enumerate(tokens):
            if YEAR_RE.match(token) and i < len(tokens) - 1:
                title_tokens = tokens[i + 1:]
                if title_tokens and title_tokens[-1] == "ocr":   # scan marker, not title
                    title_tokens = title_tokens[:-1]
                # Guard against look-alikes: "writeup-2026-02-22" (a date, not a
                # title) and "econ257-2026-syllabus" (a course code, not a surname).
                if all(t.isdigit() for t in title_tokens) or not all(t.isalpha() for t in tokens[:i]):
                    break
                return NameGuess(
                    authors=tokens[:i],
                    year=int(token),
                    title=" ".join(title_tokens) or None,
                    style="convention",
                )

    # 3. JSTOR downloads: Author-CamelCaseTitle-Year.
    if m := re.fullmatch(r"([A-Z][A-Za-z']+)-([A-Za-z]+)-(\d{4})", stem):
        return NameGuess(
            authors=[m.group(1)], year=int(m.group(3)),
            title=_split_camel(m.group(2)), style="jstor",
        )

    # 3b. Year in brackets: "Saez (2002, JPubE)", "Atkinson and Stiglitz (2015, LPE)",
    #     "Angeletos Werning (AER 2006)". Authors come before the bracket.
    if m := re.fullmatch(r"(.+?)\s*\(([^)]*)\)\s*(.*)", stem):
        year_match = re.search(r"\b(1[6-9]\d\d|20\d\d)[a-z]?\b", m.group(2))
        names = [
            t for t in re.split(r"[\s,&]+", m.group(1))
            if t and t.lower() not in NOT_AUTHORS
        ]
        if year_match and names and all(t[0].isupper() for t in names) and len(names) <= 6:
            return NameGuess(
                authors=names, year=int(year_match.group(1)),
                title=m.group(3).strip(" -") or None, style="loose",
            )

    # 4. Loose names: "Allen et al 2017", "Braghieri_Levy_Makarin_AER_2022",
    #    "KR 2008 Happiness". Capitalised words before the year are authors.
    tokens = [t for t in re.split(r"[\s_,]+", stem) if t]
    for i, token in enumerate(tokens):
        if YEAR_RE.match(token) and i >= 1:
            before, after = tokens[:i], tokens[i + 1:]
            authors = [t for t in before if t.lower() not in NOT_AUTHORS]
            if authors and all(t[0].isupper() and t.isalpha() for t in authors) and len(authors) <= 6:
                return NameGuess(
                    authors=authors, year=int(token),
                    title=" ".join(after).strip(" -") or None, style="loose",
                )
            break

    # 5. Nothing recognisable: the cleaned name is the best available title.
    return NameGuess(title=re.sub(r"[_]+", " ", stem) or None, style="none")


# --- Building the canonical name --------------------------------------------

def slugify(text: str) -> str:
    """Lowercase ASCII with hyphens: "Kőszegi & Rabin" -> "koszegi-rabin"."""
    text = text.translate(SPECIAL_LETTERS)
    # NFKD splits "ő" into "o" + a combining accent; the accent is then dropped.
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"['’`]", "", text.lower())       # O'Donoghue -> odonoghue
    return re.sub(r"[^a-z0-9]+", "-", text).strip("-")


def canonical_name(
    surnames: list[str], year: int | None, title: str,
    *, max_words: int = 6, ocr: bool = False,
) -> str:
    """Build `surname[-surname-surname]-year-short-title.pdf`.

    Four or more authors become `first-et-al`. The title is cut at a colon
    (subtitle dropped), limited to `max_words`, and never ends on a filler word.
    """
    authors = [slugify(name) for name in surnames if slugify(name)]
    if len(authors) >= 4:
        authors = [authors[0], "et", "al"]

    main_title = re.split(r"[:?]", title, maxsplit=1)[0]
    words = slugify(main_title).split("-")
    if words and words[0] in {"a", "an", "the"}:     # leading article adds nothing
        words = words[1:]
    words = words[:max_words]
    while len(words) > 1 and words[-1] in STOPWORDS:  # avoid "...-models-of"
        words.pop()

    parts = authors + ([str(year)] if year else []) + words + (["ocr"] if ocr else [])
    return "-".join(p for p in parts if p) + ".pdf"
