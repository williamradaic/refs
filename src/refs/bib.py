"""BibTeX entries from works.

Written for biblatex with biber and UTF-8 input (the setup in tex-style/wrp.sty),
so accented letters are left as they are rather than turned into LaTeX escapes.
"""

from __future__ import annotations

import re

_SPECIAL = str.maketrans({"&": r"\&", "%": r"\%", "#": r"\#", "_": r"\_"})


def _tex(text: str) -> str:
    """Escape the few characters that would break a .bib field."""
    return str(text).translate(_SPECIAL)


def _entry_type(meta: dict) -> str:
    kind, record_type = meta.get("kind"), meta.get("type") or ""
    if kind == "book":
        return "book"
    if record_type == "journal-article" and not meta.get("is_working_paper"):
        return "article"
    if record_type in {"book", "monograph", "edited-book", "reference-book"} or kind == "book":
        return "book"
    if record_type == "book-chapter" or kind == "chapter":
        return "incollection"
    if record_type in {"report", "posted-content"} and meta.get("venue") is None and \
            (meta.get("doi") or "").startswith("10.3386/"):
        return "techreport"
    return "unpublished" if record_type != "journal-article" else "article"


def entry(work: dict) -> str:
    """One BibTeX entry for a work."""
    meta = work["meta"]
    kind = _entry_type(meta)
    fields: list[tuple[str, str]] = []

    authors = " and ".join(
        f"{a['family']}, {a['given']}" if a.get("given") else a["family"]
        for a in meta.get("authors") or [] if a.get("family")
    )
    if authors:
        fields.append(("author", _tex(authors)))
    fields.append(("title", _tex(meta.get("title") or "")))
    if meta.get("year"):
        fields.append(("year", str(meta["year"])))

    doi = meta.get("doi") or ""
    if kind == "article":
        fields += [("journal", _tex(meta.get("venue") or ""))]
    elif kind == "incollection":
        fields += [("booktitle", _tex(meta.get("venue") or ""))]
    elif kind == "techreport":
        # NBER registers working paper NNNNN as DOI 10.3386/wNNNNN.
        fields += [("institution", "National Bureau of Economic Research"),
                   ("type", "Working Paper"), ("number", doi.rsplit("/w", 1)[-1])]
    elif kind == "unpublished":
        fields.append(("note", _tex(meta.get("venue") or "Working paper")))

    for name, key in (("volume", "volume"), ("number", "issue"), ("publisher", "publisher")):
        if meta.get(key) and not (kind in {"techreport", "unpublished"}) \
                and not (name == "publisher" and kind == "article"):
            fields.append((name, _tex(meta[key])))
    if meta.get("pages") and kind in {"article", "incollection"}:
        fields.append(("pages", re.sub(r"\s*[-–]+\s*", "--", meta["pages"])))
    if doi:
        fields.append(("doi", doi))            # verbatim: biblatex prints DOIs as-is

    width = max(len(name) for name, _ in fields)
    body = ",\n".join(f"  {name:<{width}} = {{{value}}}" for name, value in fields if value)
    return f"@{kind}{{{work['citekey']},\n{body}\n}}"
