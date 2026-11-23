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
import os
import re
import threading
from pathlib import Path
from urllib.parse import quote

from . import collection, reading
from .config import Config

# Front matter: a first line of exactly `---`, then everything up to the next `---` line.
_FRONT = re.compile(r"\A---\n.*?\n---\n?", re.S)
HIGHLIGHTS_START = "<!-- refs:highlights:start -->"
HIGHLIGHTS_END = "<!-- refs:highlights:end -->"
_HIGHLIGHTS_BLOCK = re.compile(
    rf"{re.escape(HIGHLIGHTS_START)}.*?{re.escape(HIGHLIGHTS_END)}", re.S
)
_WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)(?:[|#][^\]]*)?\]\]")
_SKIP_VAULT_DIRS = {".obsidian", ".trash", ".git", "node_modules"}

# Stat-validated cache of wikilinks per vault markdown file: path -> ((ino, size, mtime_ns), frozenset[target])
_backlink_cache: dict[str, tuple[tuple[int, int, int], frozenset[str]]] = {}
_backlink_lock = threading.Lock()


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


def existing_citekeys(cfg: Config) -> set[str]:
    """Citekeys that have a reading note file on disk (single scandir, fast)."""
    note_dir = folder(cfg)
    if note_dir is None or not note_dir.is_dir():
        return set()
    found: set[str] = set()
    try:
        with os.scandir(note_dir) as entries:
            for entry in entries:
                if entry.name.endswith(".md") and not entry.name.startswith("."):
                    found.add(entry.name[:-3])
    except OSError:
        pass
    return found


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


# --- Safe managed front-matter sync --------------------------------------------------------

_MANAGED_KEYS = ("title", "authors", "year", "venue", "doi", "citekey", "refs",
                 "collections", "read_status", "aliases")
_YAML_KEY_RE = re.compile(r"^([A-Za-z0-9_-]+)\s*:(.*)$")


def _managed_blocks(work: dict, collections: list[str] | None = None) -> dict[str, list[str]]:
    """Build YAML line blocks for each refs-managed front-matter property."""
    meta = work.get("meta") or {}
    authors = [" ".join(filter(None, [a.get("given"), a.get("family")]))
               for a in meta.get("authors") or []]
    surnames = [a["family"] for a in meta.get("authors") or [] if a.get("family")]
    names = " and ".join(surnames) if len(surnames) <= 2 else f"{surnames[0]} et al."
    cols = collections if collections is not None else collection.explicit(work)
    read_stat, _ = reading.effective(work)

    blocks: dict[str, list[str]] = {
        "title": [f"title: {json.dumps(meta.get('title') or '', ensure_ascii=False)}"],
    }
    if authors:
        blocks["authors"] = ["authors:", *(f"  - {json.dumps(a, ensure_ascii=False)}" for a in authors)]
    if meta.get("year"):
        blocks["year"] = [f"year: {meta['year']}"]
    if meta.get("venue"):
        blocks["venue"] = [f"venue: {json.dumps(meta['venue'], ensure_ascii=False)}"]
    if meta.get("doi"):
        blocks["doi"] = [f"doi: {json.dumps(meta['doi'])}"]
    blocks["citekey"] = [f"citekey: {work['citekey']}"]
    blocks["refs"] = [f"refs: {work['id']}"]
    if cols:
        blocks["collections"] = ["collections:", *(f"  - {json.dumps(c, ensure_ascii=False)}" for c in cols)]
    if read_stat:
        blocks["read_status"] = [f"read_status: {read_stat}"]
    if names:
        blocks["aliases"] = ["aliases:", f"  - {json.dumps(f'{names} ({meta.get('year') or 'n.d.'})', ensure_ascii=False)}"]
    return blocks


def _merge_front_matter(existing_front: str, work: dict, collections: list[str] | None = None) -> str:
    """Update only refs-managed keys in YAML front matter, preserving all user keys and order."""
    managed = _managed_blocks(work, collections)
    if not existing_front.startswith("---\n"):
        lines = ["---"]
        for key in _MANAGED_KEYS:
            if key in managed:
                lines += managed[key]
        return "\n".join(lines) + "\n---\n"

    inner = existing_front[4:]
    if inner.endswith("---\n"):
        inner = inner[:-4]
    elif inner.endswith("---"):
        inner = inner[:-3]

    # Parse top-level key blocks while preserving user lines and comments verbatim.
    raw_lines = inner.splitlines()
    entries: list[tuple[str | None, list[str]]] = []
    for line in raw_lines:
        m = _YAML_KEY_RE.match(line)
        if m and not line.startswith((" ", "\t", "-")):
            entries.append((m.group(1), [line]))
        elif entries:
            entries[-1][1].append(line)
        else:
            entries.append((None, [line]))

    out_lines = ["---"]
    placed: set[str] = set()
    for key, lines in entries:
        if key in _MANAGED_KEYS:
            if key in placed:
                continue
            placed.add(key)
            if key == "aliases" and key in managed:
                # Preserve any extra user aliases while keeping the refs alias.
                new_alias = managed["aliases"][1]
                kept = [l for l in lines if l.strip()]
                if new_alias not in kept:
                    kept.append(new_alias)
                out_lines += kept
            elif key in managed:
                out_lines += managed[key]
        else:
            out_lines += lines

    # Append managed keys that were missing before.
    for key in _MANAGED_KEYS:
        if key not in placed and key in managed:
            out_lines += managed[key]

    return "\n".join(out_lines) + "\n---\n"


def sync_front_matter(cfg: Config, work: dict, *, collections: list[str] | None = None) -> dict:
    """Update refs-managed YAML properties on an existing note without touching user keys or prose."""
    if not enabled(cfg):
        raise FileNotFoundError(f"no Obsidian vault at {cfg.obsidian_vault}"
                                if cfg.obsidian_vault else "notes are off: no Obsidian vault is set")
    path = path_for(cfg, work)
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        raw = None
    if raw is None:
        front = _merge_front_matter("", work, collections)
        text = front
    else:
        front, body = _split(raw.decode("utf-8", errors="replace"))
        new_front = _merge_front_matter(front, work, collections)
        text = new_front + body
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)
    return read(cfg, work)


# --- Pulling PDF highlights into a note (idempotent delimited block) ----------------------

def pull_highlights(cfg: Config, work: dict, items: list[dict], version: str,
                    *, collections: list[str] | None = None) -> dict:
    """Insert or refresh the delimited PDF Highlights block in the paper's Obsidian note."""
    from . import highlights

    if not enabled(cfg):
        raise FileNotFoundError(f"no Obsidian vault at {cfg.obsidian_vault}"
                                if cfg.obsidian_vault else "notes are off: no Obsidian vault is set")
    if not items:
        return read(cfg, work)
    path = path_for(cfg, work)
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        raw = None
    if _version(raw) != version:
        raise Conflict(str(path))

    block = f"{HIGHLIGHTS_START}\n{highlights.format_markdown(items)}\n{HIGHLIGHTS_END}"
    if raw is None:
        front = _merge_front_matter("", work, collections)
        body = block
    else:
        front, existing_body = _split(raw.decode("utf-8", errors="replace"))
        existing_body = existing_body.lstrip("\n")
        if _HIGHLIGHTS_BLOCK.search(existing_body):
            body = _HIGHLIGHTS_BLOCK.sub(lambda _: block, existing_body, count=1)
        elif existing_body.strip():
            body = existing_body.rstrip("\n") + "\n\n" + block
        else:
            body = block

    text = front + "\n" + body.strip("\n") + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)
    return read(cfg, work)


# --- Vault backlinks (notes referencing [[citekey]]) --------------------------------------

def _links_in_file(path: Path, st: os.stat_result) -> frozenset[str]:
    key = str(path)
    sig = (st.st_ino, st.st_size, st.st_mtime_ns)
    with _backlink_lock:
        cached = _backlink_cache.get(key)
    if cached is not None and cached[0] == sig:
        return cached[1]
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return frozenset()
    targets: set[str] = set()
    for match in _WIKILINK_RE.finditer(text):
        target = match.group(1).strip().lower()
        if target:
            targets.add(target)
            if "/" in target:
                targets.add(target.rsplit("/", 1)[-1])
    frozen = frozenset(targets)
    with _backlink_lock:
        if len(_backlink_cache) > 16384:
            _backlink_cache.clear()
        _backlink_cache[key] = (sig, frozen)
    return frozen


def backlinks(cfg: Config, citekey: str) -> list[dict]:
    """Other Markdown notes in the Obsidian vault that link to `[[citekey]]`."""
    if not citekey or not enabled(cfg) or cfg.obsidian_vault is None:
        return []
    vault = cfg.obsidian_vault
    own_note = (vault / cfg.obsidian_folder / f"{citekey}.md").resolve()
    wanted = citekey.strip().lower()
    found: list[dict] = []
    try:
        for root, dirs, files in os.walk(vault, followlinks=False):
            dirs[:] = [d for d in dirs if d not in _SKIP_VAULT_DIRS and not d.startswith(".")]
            for name in files:
                if not name.endswith(".md") or name.startswith("."):
                    continue
                full = Path(root) / name
                try:
                    if full.resolve() == own_note:
                        continue
                    st = full.stat()
                except OSError:
                    continue
                if wanted in _links_in_file(full, st):
                    rel = full.relative_to(vault).as_posix()
                    rel_no_ext = rel[:-3]
                    found.append({
                        "title": full.stem,
                        "file": rel,
                        "uri": f"obsidian://open?vault={quote(vault.name)}&file={quote(rel_no_ext)}",
                    })
    except OSError:
        return []
    found.sort(key=lambda item: item["file"].lower())
    return found
