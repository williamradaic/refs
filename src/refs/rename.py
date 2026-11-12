"""Renaming files to the house convention, with a journal so it can be undone.

Rules (from the plan):
  * only verified works are renamed: the name is built from registry metadata;
  * in place only: a rename never moves a file to another folder;
  * never inside folders other tools manage (canvas-sync, overleaf) or a Git repo;
  * every rename is appended to <state>/journal.jsonl and can be reversed.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from . import naming
from .config import Config


def journal_path(cfg: Config) -> Path:
    return cfg.absolute(cfg.state_dir) / "journal.jsonl"


def proposed_name(work: dict, current_name: str, *, allow_unverified: bool = False) -> str | None:
    """The canonical file name for a work, or None if it must not be renamed.

    `allow_unverified` is only for a file the app itself just downloaded under a
    temporary name: a name built from the model's reading beats "added-93bc.pdf".
    """
    meta = work.get("meta") or {}
    surnames = [a["family"] for a in meta.get("authors") or [] if a.get("family")]
    if not surnames or not meta.get("title"):
        return None
    if meta.get("status") not in {"verified", "confirmed"} and not allow_unverified:
        return None
    # Keep an existing "-ocr" marker: it says the file is a scan with added text.
    is_ocr = PurePosixPath(current_name).stem.lower().endswith("ocr")
    return naming.canonical_name(surnames, meta.get("year"), meta["title"], ocr=is_ocr)


def disambiguate(name: str, taken: set[str], is_working_paper: bool) -> str:
    """Pick a free name when two different PDFs of one paper sit in the same folder.

    The first file to claim a name keeps it (callers offer published versions
    first). A working-paper version then becomes "...-wp.pdf"; anything else,
    or a second working paper, gets "-2", "-3", ...
    """
    if name not in taken:
        return name
    stem = name.removesuffix(".pdf")
    if is_working_paper and f"{stem}-wp.pdf" not in taken:
        return f"{stem}-wp.pdf"
    number = 2
    while f"{stem}-{number}.pdf" in taken:
        number += 1
    return f"{stem}-{number}.pdf"


def free_name(cfg: Config, rel: str, new_name: str, is_working_paper: bool) -> str | None:
    """The name one file should get in its own folder, or None to leave it as is.

    Other PDFs already in the folder keep their names; the file's own current
    name does not count as taken, since renaming frees it. So a working paper
    already called "...-wp.pdf" next to the published "....pdf" stays put
    instead of becoming "...-2.pdf".
    """
    current = PurePosixPath(rel).name
    folder = cfg.absolute(rel).parent
    taken = {p.name for p in folder.glob("*.pdf")} - {current}
    final = disambiguate(new_name, taken, is_working_paper)
    return None if final == current else final


def blocked_reason(cfg: Config, rel: str) -> str | None:
    """Why this file must be left alone, or None if renaming is allowed."""
    parts = PurePosixPath(rel).parts[:-1]
    for folder in parts:
        if folder.lower() in {name.lower() for name in cfg.no_rename}:
            return f"inside a managed folder ({folder})"
    # Walk up from the file's folder: a .git anywhere above means a repository.
    folder = cfg.absolute(rel).parent
    while folder != cfg.dropbox_root and folder != folder.parent:
        if (folder / ".git").exists():
            return "inside a Git repository"
        folder = folder.parent
    return None


def apply(cfg: Config, rel: str, new_name: str, file_hash: str | None = None) -> str | None:
    """Rename one file in place. Returns the new relative path, or None if skipped."""
    old = cfg.absolute(rel)
    new = old.with_name(new_name)
    if old.name == new_name or blocked_reason(cfg, rel) or not old.exists():
        return None
    if new.exists():                      # never overwrite: another file has that name
        return None
    os.rename(old, new)
    new_rel = str(PurePosixPath(rel).with_name(new_name))
    journal_path(cfg).parent.mkdir(parents=True, exist_ok=True)
    with journal_path(cfg).open("a") as journal:
        journal.write(json.dumps({
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "from": rel, "to": new_rel, "hash": file_hash,
        }, ensure_ascii=False) + "\n")
    return new_rel


def undo(cfg: Config, count: int = 1) -> list[tuple[str, str]]:
    """Reverse the last `count` renames. Returns (restored path, path it had)."""
    if not journal_path(cfg).exists():
        return []
    lines = journal_path(cfg).read_text().splitlines()
    restored = []
    while lines and len(restored) < count:
        entry = json.loads(lines.pop())
        current, original = cfg.absolute(entry["to"]), cfg.absolute(entry["from"])
        if current.exists() and not original.exists():
            os.rename(current, original)
            restored.append((entry["from"], entry["to"]))
    journal_path(cfg).write_text("".join(line + "\n" for line in lines))
    return restored
