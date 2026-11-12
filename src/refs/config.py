"""Settings: where Dropbox is, which folders to scan, and what to leave alone.

Defaults live in this file. To change one, create `~/.config/refs/config.toml`
and set only the keys you want to override, for example:

    roots = ["phd", "papers-dump", "Theory Reading Group"]

All folder settings are written relative to the Dropbox root (never as
`/Users/...`), so the same settings work on another Mac or on a server.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

# --- Defaults ---------------------------------------------------------------

DEFAULTS: dict = {
    # Folders (relative to the Dropbox root) that are scanned for PDFs.
    "roots": ["phd", "papers-dump"],
    # The quick-add folder: the one place the app is allowed to rename freely.
    "dump": "papers-dump",
    # Where the app keeps its own record files inside Dropbox. Never scanned.
    "state_dir": "phd/tools/refs",
    # Folders left out entirely: visa, immigration and insurance paperwork.
    # They are not references, and must never reach an index or an LLM.
    "exclude_paths": ["phd/admission", "phd/move-in"],
    # Folder names skipped everywhere (compared case-insensitively). Figure
    # folders hold plots, not documents; `_versions` holds old canvas-sync copies.
    "ignore_dirs": [
        ".git", ".claude", ".venv", "node_modules", "__pycache__",
        "_versions", "fig", "figs", "figures",
    ],
    # Path prefixes whose contents are never sent to an LLM (confidential).
    "no_llm": ["phd/referee"],
    # Folder names whose files the app must never rename (managed by other tools).
    "no_rename": ["canvas-sync", "overleaf"],
    # Optional. Crossref and OpenAlex allow more requests per second to clients
    # that identify themselves with an email. Empty = send none.
    "contact_email": "",
}


@dataclass(frozen=True)
class Config:
    """Resolved settings for one run of the program."""

    dropbox_root: Path          # absolute, symlinks resolved
    index_db: Path              # local SQLite index (rebuildable cache)
    roots: list[str] = field(default_factory=list)
    dump: str = "papers-dump"
    state_dir: str = "phd/tools/refs"
    exclude_paths: list[str] = field(default_factory=list)
    ignore_dirs: frozenset[str] = frozenset()
    no_llm: list[str] = field(default_factory=list)
    no_rename: list[str] = field(default_factory=list)

    def absolute(self, rel: str) -> Path:
        """Turn a Dropbox-relative path into a real path on this machine."""
        return self.dropbox_root / rel


# --- Loading ----------------------------------------------------------------

def _find_dropbox_root() -> Path:
    """Locate the Dropbox folder: modern macOS location first, then the classic one."""
    candidates = [
        Path.home() / "Library" / "CloudStorage" / "Dropbox",
        Path.home() / "Dropbox",
    ]
    for candidate in candidates:
        if candidate.is_dir():
            # resolve() follows symlinks, so ~/Dropbox and the CloudStorage path
            # end up as the same string. Every stored path is relative to this.
            return candidate.resolve()
    raise SystemExit(
        "Could not find a Dropbox folder. Set REFS_DROPBOX_ROOT or add "
        "`dropbox_root = \"/path/to/Dropbox\"` to ~/.config/refs/config.toml."
    )


def load() -> Config:
    """Build the Config from defaults, then the optional file, then environment.

    Environment variables (used mainly by the tests):
      REFS_CONFIG        path to a config file other than ~/.config/refs/config.toml
      REFS_DROPBOX_ROOT  Dropbox folder to use
      REFS_INDEX_DB      where to keep the SQLite index
    """
    settings = dict(DEFAULTS)

    # Optional user file: only the keys present there replace the defaults.
    config_file = Path(
        os.environ.get("REFS_CONFIG", Path.home() / ".config" / "refs" / "config.toml")
    )
    if config_file.is_file():
        with config_file.open("rb") as handle:
            settings.update(tomllib.load(handle))

    root_setting = os.environ.get("REFS_DROPBOX_ROOT") or settings.get("dropbox_root")
    dropbox_root = Path(root_setting).expanduser().resolve() if root_setting else _find_dropbox_root()

    # The index is a cache and stays OUT of Dropbox: syncing a live SQLite file
    # between machines corrupts it.
    index_setting = os.environ.get("REFS_INDEX_DB") or settings.get("index_db")
    index_db = (
        Path(index_setting).expanduser()
        if index_setting
        else Path.home() / "Library" / "Application Support" / "refs" / "index.db"
    )

    return Config(
        dropbox_root=dropbox_root,
        index_db=index_db,
        roots=list(settings["roots"]),
        dump=settings["dump"],
        state_dir=settings["state_dir"],
        exclude_paths=list(settings["exclude_paths"]),
        ignore_dirs=frozenset(name.lower() for name in settings["ignore_dirs"]),
        no_llm=list(settings["no_llm"]),
        no_rename=list(settings["no_rename"]),
    )


def contact_email() -> str:
    """The email to show registries, if you chose to set one in config.toml."""
    config_file = Path(
        os.environ.get("REFS_CONFIG", Path.home() / ".config" / "refs" / "config.toml"))
    try:
        with config_file.open("rb") as handle:
            return str(tomllib.load(handle).get("contact_email", "")).strip()
    except (OSError, tomllib.TOMLDecodeError):
        return ""
