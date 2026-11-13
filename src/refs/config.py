"""Settings: where Dropbox is, which folders to scan, and what to leave alone.

Defaults live in this file. To change one, create `~/.config/refs/config.toml`
and set only the keys you want to override, for example:

    roots = ["phd", "papers-dump", "Theory Reading Group"]

The lists that keep files away from the app (exclude_paths, ignore_dirs,
no_llm, no_rename) are the exception: entries in the file are added to the
defaults, never in place of them.

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
    # Single files can be listed too (say, a transcript inside a course folder).
    "exclude_paths": ["phd/admission", "phd/move-in"],
    # Folder names skipped everywhere (compared case-insensitively). Figure
    # folders hold plots, not documents; `_versions` holds old canvas-sync copies.
    # `admin` is where paperwork goes (reimbursements, contracts, the `local/admin`
    # of the project layout): any folder of that name is never read.
    "ignore_dirs": [
        ".git", ".claude", ".venv", "node_modules", "__pycache__",
        "_versions", "fig", "figs", "figures", "admin",
    ],
    # Path prefixes whose contents are never sent to an LLM (confidential).
    "no_llm": ["phd/referee"],
    # Folder names whose files the app must never rename (managed by other tools).
    "no_rename": ["canvas-sync", "overleaf"],
    # What the folder watcher (part of `refs serve`) reads when a new PDF
    # appears: "all" = new PDFs anywhere under the roots (about $0.001 each),
    # "dump" = only those saved into the dump, "none" = keep the index current
    # but leave reading to `refs process`. Renaming happens only in the dump.
    "auto_process": "all",
    # Project .bib files whose citation keys refs learns (bibkeys.py), so a
    # paper cited as `BLP95` in a project is found by that key too. Read only.
    # "~/..." or "/..." = a path on this Mac; anything else = inside Dropbox.
    # `**` = any depth. Course and TA folders are left out on purpose: their
    # .bib files are someone else's keys.
    "bib_files": [
        "~/phd/research/*/overleaf/**/*.bib",
        "~/phd/research/*/*.bib",
        "~/phd/ra/*/overleaf/**/*.bib",
        "~/phd/projects/*/overleaf/**/*.bib",
        "phd/research/**/*.bib",
    ],
    # Optional. Crossref and OpenAlex allow more requests per second to clients
    # that identify themselves with an email. Empty = send none.
    "contact_email": "",
    # Reading notes (notes.py): one Markdown file per paper, named by citekey,
    # in this folder of this Obsidian vault. The vault is a path on this Mac
    # ("~/..."), not in Dropbox; the folder is relative to the vault.
    "obsidian_vault": "~/wr-vault",
    "obsidian_folder": "phd/refs",
    # Semantic search (embed.py): the local model that turns each paper's text
    # into a vector. Changing it re-embeds the library at the next scan (about
    # a minute). "BAAI/bge-base-en-v1.5" is the larger, somewhat better option.
    "embedding_model": "BAAI/bge-small-en-v1.5",
    # Asking Haiku (ask.py) to rank candidates for a search that reads like a
    # description ("IO paper on ice cream"). "auto" = only when the local
    # results look unsure, or when the query starts with "?"; "never" = never.
    "ask": "auto",
    # Dollars per calendar month that asking may spend (about $0.0005 a question).
    # Once reached, searches stay local until the next month.
    "ask_monthly_cap": 1.0,
}


# Settings whose config.toml values are added to the defaults instead of replacing them.
PROTECTIVE = ("exclude_paths", "ignore_dirs", "no_llm", "no_rename")


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
    auto_process: str = "all"
    bib_files: list[str] = field(default_factory=list)
    obsidian_vault: Path = Path.home() / "wr-vault"
    obsidian_folder: str = "phd/refs"
    embedding_model: str = "BAAI/bge-small-en-v1.5"
    ask: str = "auto"
    ask_monthly_cap: float = 1.0

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
            user = tomllib.load(handle)
        # Lists that protect files only ever grow: what config.toml lists is
        # added to the defaults, so excluding one more folder there can never
        # quietly un-exclude the default ones.
        for key in PROTECTIVE:
            if key in user:
                user[key] = list(dict.fromkeys([*DEFAULTS[key], *user[key]]))
        settings.update(user)

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
        bib_files=list(settings["bib_files"]),
        obsidian_vault=Path(settings["obsidian_vault"]).expanduser(),
        obsidian_folder=str(settings["obsidian_folder"]).strip("/"),
        auto_process=_choice(settings["auto_process"], "auto_process", {"all", "dump", "none"}),
        embedding_model=str(settings["embedding_model"]),
        ask=_choice(settings["ask"], "ask", {"auto", "never"}),
        ask_monthly_cap=float(settings["ask_monthly_cap"]),
    )


def _choice(value: str, key: str, allowed: set[str]) -> str:
    """A setting that must be one of a few words; say so clearly if it is not."""
    if value not in allowed:
        raise SystemExit(f"config.toml: {key} must be one of {', '.join(sorted(allowed))}, not {value!r}.")
    return value


def contact_email() -> str:
    """The email to show registries, if you chose to set one in config.toml."""
    config_file = Path(
        os.environ.get("REFS_CONFIG", Path.home() / ".config" / "refs" / "config.toml"))
    try:
        with config_file.open("rb") as handle:
            return str(tomllib.load(handle).get("contact_email", "")).strip()
    except (OSError, tomllib.TOMLDecodeError):
        return ""
