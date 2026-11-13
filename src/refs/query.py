"""Small, deterministic judgements about a search query.

Three questions, answered from the words alone (no model, no index):

  * abbreviation()  what a field or method abbreviation ("IO", "RDD") stands for
  * is_lookup()     is a word an identifier (citekey, DOI, NBER number, initials)
                    rather than an ordinary word?
  * sentence_like() does the query read like a description of a paper
                    ("IO paper on ice cream") rather than a lookup ("blp 1995")?

ask.py uses sentence_like() as the first of its two signals for sending a
search to Haiku; index.py uses abbreviation() and is_lookup() to shape the search.
"""

from __future__ import annotations

import re

# --- Abbreviations ------------------------------------------------------------------

# What economists abbreviate. Matching ignores case, except for the entries in
# UPPERCASE_ONLY, which are also ordinary English words ("be", "did") and are
# expanded only when typed in capitals ("BE", "DiD").
ABBREVIATIONS = {
    "io": "industrial organization",
    "pf": "public finance",
    "pe": "public economics",
    "be": "behavioral economics",
    "macro": "macroeconomics",
    "micro": "microeconomics",
    "metrics": "econometrics",
    "dev": "development economics",
    "did": "difference-in-differences",
    "rd": "regression discontinuity",
    "rdd": "regression discontinuity",
    "iv": "instrumental variables",
    "rct": "randomized controlled trial",
    "ge": "general equilibrium",
    "dsge": "dynamic stochastic general equilibrium",
    "ui": "unemployment insurance",
    "eitc": "earned income tax credit",
    "blp": "Berry Levinsohn Pakes demand estimation",
}
UPPERCASE_ONLY = {"be", "did", "pe", "dev", "ge"}


def abbreviation(word: str) -> str | None:
    """The expansion of one word, or None if it is not an abbreviation we know."""
    key = word.lower()
    if key not in ABBREVIATIONS:
        return None
    if key in UPPERCASE_ONLY and not any(c.isupper() for c in word):
        return None
    return ABBREVIATIONS[key]


# --- Identifiers ----------------------------------------------------------------------

_ID_PATTERNS = [
    re.compile(r"^[a-z'\-]+\d{4}[a-z]*$", re.I),     # citekey: odonoghue2006optimal, BLP95-style keys too
    re.compile(r"^[a-z]+\d{2}$", re.I),               # short project keys: blp95, dk11
    re.compile(r"^10\.\d{4,}/\S+$"),                  # DOI
    re.compile(r"^w\d{4,5}$", re.I),                  # NBER working paper
    re.compile(r"^\d{4}\.\d{4,5}(v\d+)?$"),           # arXiv
    re.compile(r"^(18|19|20)\d{2}$"),                 # a year
]
_VOWELS = set("aeiouy")


def is_lookup(word: str, surnames: set[str] = frozenset()) -> bool:
    """Is this word an identifier or a name rather than an ordinary word?

    `surnames` is the set of author surnames in the library (lowercase), so
    that "chetty" counts as a name and not as a word to understand.
    """
    w = word.lower().strip(".,;:!?\"'()")
    if not w:
        return True
    if any(p.match(w) for p in _ID_PATTERNS):
        return True
    if w in surnames:
        return True
    # Author initials ("blp", "ddk"): short and without vowels.
    return 2 <= len(w) <= 5 and w.isalpha() and not (set(w) & _VOWELS)


# --- Does the query describe a paper? ---------------------------------------------

# Words people use when describing rather than naming ("the paper on ...").
FILLER = {
    "paper", "papers", "article", "study", "one", "about", "on", "that", "which",
    "where", "with", "the", "a", "an", "of", "for", "in", "by", "some", "using",
    "shows", "showing", "finds", "find", "looks", "at", "how", "why", "what",
    "whether", "is", "was", "and", "to", "from",
}


def sentence_like(terms: list[str], surnames: set[str] = frozenset()) -> bool:
    """True when the query reads like a description, not a lookup.

    It needs at least two content words (neither identifiers, names, nor
    describing words), and either three words in all or a describing word
    such as "on" or "about": "IO paper on ice cream" and "salience tax
    experiment" qualify; "blp", "chetty 2009", "present bias" do not.
    """
    words = [t.lstrip("?") for t in terms if t.lstrip("?")]
    content = [w for w in words if w.lower() not in FILLER and not is_lookup(w, surnames)]
    if len(content) < 2:
        return False
    has_filler = any(w.lower() in FILLER for w in words)
    return len(words) >= 3 or has_filler
