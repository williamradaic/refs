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


# --- Appending to a project's .bib file --------------------------------------------
# A project's .bib is yours (and often also edited on Overleaf), so refs only
# ever adds entries at the end, and never one for a paper that is already in
# the file, whatever key it is under there.

_ENTRY = re.compile(r"@(\w+)\s*\{\s*([^,\s]+)\s*,", re.IGNORECASE)
# A field value in braces (one level of nested braces allowed) or in quotes.
_VALUE = r"\s*=\s*(\{(?:[^{}]|\{[^{}]*\})*\}|\"[^\"]*\")"


def _norm_title(title: str) -> str:
    """Lowercase letters and digits only: "{The} Case for {AI}" == "the case for ai"."""
    return re.sub(r"[^a-z0-9]", "", title.lower())


def read_entries(text: str) -> list[dict]:
    """The entries of a .bib file: key, DOI and title of each (as far as present)."""
    entries = []
    starts = list(_ENTRY.finditer(text))
    for n, match in enumerate(starts):
        if match.group(1).lower() in {"string", "comment", "preamble"}:
            continue
        body = text[match.end():starts[n + 1].start() if n + 1 < len(starts) else len(text)]
        found = {}
        for field in ("doi", "title"):
            value = re.search(rf"\b{field}{_VALUE}", body, re.IGNORECASE)
            if value:
                found[field] = value.group(1)[1:-1].replace("{", "").replace("}", "").strip()
        entries.append({"key": match.group(2), "doi": (found.get("doi") or "").lower()
                        .removeprefix("https://doi.org/") or None,
                        "title": _norm_title(found.get("title") or "") or None})
    return entries


def already_in(work: dict, entries: list[dict]) -> str | None:
    """The key under which a .bib file already holds this work, or None.

    Same key, same DOI, or the same title (ignoring case, braces and punctuation).
    """
    meta = work.get("meta") or {}
    doi = (meta.get("doi") or "").lower()
    title = _norm_title(meta.get("title") or "")
    for existing in entries:
        if existing["key"] == work["citekey"] or (doi and existing["doi"] == doi) \
                or (title and existing["title"] == title):
            return existing["key"]
    return None


def append(path, works: list[dict]) -> tuple[list[dict], list[tuple[dict, str]]]:
    """Add the works that are not in the .bib file yet, at its end.

    Returns (works added, [(work, key it already has in the file)]).
    """
    from pathlib import Path
    path = Path(path)
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    entries = read_entries(text)
    added, present = [], []
    for work in works:
        key = already_in(work, entries)
        if key:
            present.append((work, key))
            continue
        added.append(work)
        entries.append({"key": work["citekey"], "doi": (work["meta"].get("doi") or "").lower() or None,
                        "title": _norm_title(work["meta"].get("title") or "") or None})
    if added:
        block = "\n\n".join(entry(w) for w in added) + "\n"
        # Finish the file's last line if needed, then one blank line before the new entries.
        separator = ("" if not text or text.endswith("\n") else "\n") + ("\n" if text.strip() else "")
        with path.open("a", encoding="utf-8") as handle:
            handle.write(separator + block)
    return added, present
