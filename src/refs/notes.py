"""Reading notes: one Markdown file per work, kept in the Obsidian vault.

The note for a work is `<vault>/<folder>/<citekey>.md`. Citekeys never change
(works.py), so the file name is a stable link between refs and Obsidian, and
`[[citekey]]` links to the paper from any other note.

refs writes the YAML front matter (the "properties" Obsidian shows) once, when
it creates the file, and never touches it again: properties you add in
Obsidian stay. The web page shows and edits only the text below it.

Two programs edit the same file, so every save says which version of the file
it started from (a hash of its bytes). If the file changed in between, say
because you edited it in Obsidian, the save is refused instead of overwriting.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from urllib.parse import quote

from .config import Config

# Front matter: a first line of exactly `---`, then everything up to the next `---` line.
_FRONT = re.compile(r"\A---\n.*?\n---\n?", re.S)


class Conflict(Exception):
    """The file changed after the page loaded it."""


def enabled(cfg: Config) -> bool:
    """Notes are on when a vault is set and it really is an Obsidian vault."""
    return cfg.obsidian_vault is not None and (cfg.obsidian_vault / ".obsidian").is_dir()


def folder(cfg: Config) -> Path | None:
    """The folder that holds the notes; None when no vault is set."""
    return cfg.obsidian_vault / cfg.obsidian_folder if cfg.obsidian_vault else None


def path_for(cfg: Config, work: dict) -> Path:
    return folder(cfg) / f"{work['citekey']}.md"


def _version(raw: bytes | None) -> str:
    """A short fingerprint of the file's bytes; "" for a file that does not exist."""
    return hashlib.sha256(raw).hexdigest()[:16] if raw is not None else ""


def _split(text: str) -> tuple[str, str]:
    """(front matter including its `---` lines, the rest)."""
    match = _FRONT.match(text)
    return (match.group(0), text[match.end():]) if match else ("", text)


def read(cfg: Config, work: dict) -> dict:
    """What the page needs: the text below the front matter, the version, a link."""
    if cfg.obsidian_vault is None:
        # No vault set: the page says notes are off and how to turn them on.
        return {"body": "", "version": "", "exists": False, "available": False,
                "file": "", "uri": ""}
    path = path_for(cfg, work)
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        raw = None
    body = _split(raw.decode("utf-8", errors="replace"))[1].lstrip("\n") if raw is not None else ""
    relative = f"{cfg.obsidian_folder}/{work['citekey']}"
    return {
        "body": body, "version": _version(raw), "exists": raw is not None,
        "available": enabled(cfg),
        "file": f"{relative}.md",
        # Opens (or, if it is missing, creates) the note in Obsidian.
        "uri": f"obsidian://open?vault={quote(cfg.obsidian_vault.name)}&file={quote(relative)}",
    }


def bodies(cfg: Config) -> dict[str, str]:
    """The text of every note, by citekey (front matter left out), for searching.

    Hidden files (the temporary file of a save in progress) are skipped. A
    vault that is missing or unreadable simply yields no notes.
    """
    found: dict[str, str] = {}
    if folder(cfg) is None:
        return found
    try:
        paths = sorted(folder(cfg).glob("*.md"))
    except OSError:
        return found
    for path in paths:
        if path.name.startswith("."):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if body := _split(text)[1].strip():
            found[path.stem] = body
    return found


def _front_matter(work: dict) -> str:
    """Properties for a new note. Values are written as JSON, which is valid YAML."""
    meta = work.get("meta") or {}
    authors = [" ".join(filter(None, [a.get("given"), a.get("family")]))
               for a in meta.get("authors") or []]
    surnames = [a["family"] for a in meta.get("authors") or [] if a.get("family")]
    # An alias like "O'Donoghue and Rabin (2006)" lets Obsidian's link
    # autocomplete find the note by author and year, not only by citekey.
    names = " and ".join(surnames) if len(surnames) <= 2 else f"{surnames[0]} et al."
    lines = ["---", f"title: {json.dumps(meta.get('title') or '', ensure_ascii=False)}"]
    if authors:
        lines += ["authors:", *(f"  - {json.dumps(a, ensure_ascii=False)}" for a in authors)]
    if meta.get("year"):
        lines.append(f"year: {meta['year']}")
    if meta.get("doi"):
        lines.append(f"doi: {json.dumps(meta['doi'])}")
    lines.append(f"citekey: {work['citekey']}")
    lines.append(f"refs: {work['id']}")
    if names:
        lines += ["aliases:", f"  - {json.dumps(f'{names} ({meta.get('year') or 'n.d.'})', ensure_ascii=False)}"]
    return "\n".join(lines) + "\n---\n"


def write(cfg: Config, work: dict, body: str, version: str) -> dict:
    """Save the text of a note, keeping its front matter. Returns read()'s answer.

    `version` is what read() gave the page. Raises Conflict if the file is no
    longer that version. An empty note that does not exist yet is not created.
    """
    if not enabled(cfg):
        raise FileNotFoundError(f"no Obsidian vault at {cfg.obsidian_vault}"
                                if cfg.obsidian_vault else "notes are off: no Obsidian vault is set")
    path = path_for(cfg, work)
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        raw = None
    if _version(raw) != version:
        raise Conflict(str(path))
    if raw is None and not body.strip():
        return read(cfg, work)

    front = _split(raw.decode("utf-8"))[0] if raw is not None else _front_matter(work)
    text = front + "\n" + body.strip("\n") + "\n" if body.strip() else front
    path.parent.mkdir(parents=True, exist_ok=True)
    # Write a hidden temporary file, then swap it in, so Obsidian (and Obsidian
    # Sync) never sees a half-written note. Obsidian ignores dot-files.
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)
    return read(cfg, work)
