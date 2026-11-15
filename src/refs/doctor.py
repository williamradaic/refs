"""`refs doctor` (is everything in order?) and `refs uninstall` (take refs off this Mac).

Doctor's report is what to send when asking for help: it names folders and
states, never the API key or any paper.

Uninstall removes only what refs itself created on this Mac, file by file:
the login service, the index and its lock files, the caches, the log, and
(unless kept) the settings file and the Keychain key. It never touches your
PDFs, refs' records in the library folder (works, reads: a reinstall picks
them up again), or your notes.
"""

from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

from . import __version__, config

OK, WARN, BAD = "✓", "!", "✗"


@dataclass
class Check:
    state: str          # OK, WARN or BAD
    name: str
    detail: str
    fix: str = ""


def _home(path: Path | str) -> str:
    text, home = str(path), str(Path.home())
    return "~" + text[len(home):] if text.startswith(home) else text


def checks(online: bool = True) -> list[Check]:
    """Everything doctor looks at, in order. `online` also asks Anthropic
    whether the key works (free) and the local server whether it answers."""
    from . import budget, embed, index, keys, notes, records, service
    from . import ask as asking

    out: list[Check] = [Check(OK, "refs", f"{__version__}, Python {sys.version.split()[0]}, "
                                         f"running from {_home(Path(__file__).parent)}")]
    path = config.config_path()
    try:
        cfg = config.load()
    except SystemExit as error:
        out.append(Check(BAD, "settings", f"{_home(path)}: {error}", "fix the file, or move it away and run `refs init`"))
        return out
    out.append(Check(OK if path.is_file() else WARN, "settings",
                     _home(path) if path.is_file() else "no settings file yet",
                     "" if path.is_file() else "run `refs init`, or open the page"))

    # The library and its folders.
    if problem := config.not_set_up(cfg):
        out.append(Check(BAD, "library", problem, "run `refs init`, or open the page"))
    else:
        missing = [r for r in cfg.roots if not cfg.absolute(r).is_dir() and r != cfg.dump]
        out.append(Check(BAD if missing else OK, "library",
                         f"{_home(cfg.dropbox_root)}, watching {', '.join(cfg.roots)}"
                         + (f"; missing: {', '.join(missing)}" if missing else ""),
                         "fix the watched folders on the settings page" if missing else ""))

    # The index and what it holds.
    conn = None
    try:
        conn = index.connect(cfg)
        pdfs = conn.execute("SELECT COUNT(DISTINCT hash) FROM paths").fetchone()[0]
        unread = conn.execute("SELECT COUNT(*) FROM files WHERE status IS NULL "
                              "AND hash IN (SELECT hash FROM paths)").fetchone()[0]
        out.append(Check(OK if pdfs else WARN, "index",
                         f"{pdfs:,} PDFs, {unread:,} not identified ({_home(cfg.index_db)})",
                         "" if pdfs else "run `refs scan`"))
    except Exception as error:                          # noqa: BLE001 (report, do not crash)
        out.append(Check(BAD, "index", f"cannot open {_home(cfg.index_db)}: {error}",
                         "delete the index file; `refs scan` rebuilds it"))

    # Data written by a newer refs (records.SCHEMA).
    state = cfg.absolute(cfg.state_dir)
    newer = 0
    for file in [*(state / "works").glob("*.json"), state / "review.json"]:
        data = records.read_json(file) if file.is_file() else None
        newer += bool(data and int(data.get("schema", 0)) > records.SCHEMA)
    if newer:
        out.append(Check(BAD, "data", f"{newer} records were written by a newer refs",
                         "update: `uv tool upgrade refs`"))

    # The API key.
    status = "environment" if os.environ.get("ANTHROPIC_API_KEY") else ("set" if keys.get_api_key() else "missing")
    if status == "missing":
        out.append(Check(BAD, "API key", "none: new PDFs are indexed but not read",
                         "settings page, or `refs setup-key`"))
    else:
        where = "ANTHROPIC_API_KEY" if status == "environment" else "the Keychain"
        problem = keys.problem_with(keys.get_api_key() or "") if online else None
        out.append(Check(WARN if problem else OK, "API key",
                         f"in {where}" + (f", but {problem}" if problem else ", accepted by Anthropic" if online else ""),
                         "replace it on the settings page" if problem else ""))

    # Spending this month.
    if conn is not None:
        read, ask = budget.spent_this_month(conn), asking.spent_this_month(conn)
        full = read >= cfg.read_monthly_cap
        out.append(Check(WARN if full else OK, "spending",
                         f"reading ${read:.2f} of ${cfg.read_monthly_cap:.2f} this month; "
                         f"asking ${ask:.2f} of ${cfg.ask_monthly_cap:.2f}",
                         "raise read_monthly_cap on the settings page" if full else ""))

    # Notes.
    if cfg.obsidian_vault is None:
        out.append(Check(OK, "notes", "off (no Obsidian vault set)"))
    else:
        out.append(Check(OK if notes.enabled(cfg) else WARN, "notes",
                         _home(notes.folder(cfg)) if notes.enabled(cfg) else
                         f"{_home(cfg.obsidian_vault)} is not an Obsidian vault",
                         "" if notes.enabled(cfg) else "choose the vault on the settings page"))

    # The search model (downloaded once).
    model_here = embed.CACHE_DIR.is_dir() and any(embed.CACHE_DIR.iterdir())
    out.append(Check(OK if model_here else WARN, "search model",
                     cfg.embedding_model + ("" if model_here else ": not downloaded yet"),
                     "" if model_here else "the next scan downloads it (about 70 MB)"))

    # The background service and the page.
    loaded = service.is_loaded()
    answering = _answers(service.PORT) if online else None
    if loaded:
        out.append(Check(OK if answering is not False else BAD, "service",
                         f"running at login; http://refs.localhost:{service.PORT} "
                         + ("answers" if answering else "does NOT answer" if answering is False else ""),
                         "" if answering is not False else f"see {_home(service.LOG)}; `refs service restart`"))
    else:
        out.append(Check(WARN, "service", "not running in the background",
                         "`refs service install` starts it now and at login"))
    out.append(Check(OK, "log", _home(service.LOG)))
    if conn is not None:
        conn.close()
    return out


def _answers(port: int) -> bool:
    """Does refs (not something else) answer on this port?"""
    import httpx
    try:
        response = httpx.get(f"http://127.0.0.1:{port}/api/changes", timeout=2,
                             headers={"Host": "localhost"})
        return response.status_code == 200 and "generation" in response.json()
    except Exception:                                   # noqa: BLE001 (not answering)
        return False


# --- uninstall ---------------------------------------------------------------------

def leftovers(cfg: config.Config, keep_settings: bool, keep_key: bool) -> list[tuple[str, Path | None]]:
    """What uninstall removes: (description, path) pairs; path None = not a file."""
    from . import embed, keys, service
    support = cfg.index_db.parent
    items: list[tuple[str, Path | None]] = [("the login service", service.PLIST)]
    for name in [cfg.index_db.name, cfg.index_db.name + "-wal", cfg.index_db.name + "-shm",
                 "ingest.lock", "watch.lock"]:
        items.append(("the index" if name.startswith(cfg.index_db.name) else "a lock file", support / name))
    items.append(("caches (search model, registry answers)", embed.CACHE_DIR.parent))
    items.append(("the log", service.LOG))
    if not keep_settings:
        path = config.config_path()
        items += [("your settings", path), ("the settings backup", path.with_name(path.name + ".bak"))]
    if not keep_key and keys.get_api_key() and not os.environ.get("ANTHROPIC_API_KEY"):
        items.append(("the API key in the Keychain", None))
    return [(what, p) for what, p in items if p is None or p.exists()]


def uninstall(cfg: config.Config, keep_settings: bool, keep_key: bool) -> list[str]:
    """Remove what refs created on this Mac. Returns what was removed."""
    from . import embed, keys, service
    done = []
    for what, path in leftovers(cfg, keep_settings, keep_key):
        if path is None:
            import keyring
            keyring.delete_password(keys.SERVICE, keys.ACCOUNT)
        elif path == service.PLIST:
            service.uninstall()
        elif path == embed.CACHE_DIR.parent and path.name == "refs":
            shutil.rmtree(path)                     # ~/Library/Caches/refs, refs' own
        else:
            path.unlink()
        done.append(f"{what}" + (f": {_home(path)}" if path is not None else ""))
    # The support folder goes too, if refs' files were all that was in it.
    support = cfg.index_db.parent
    if support.name == "refs" and support.is_dir() and not any(support.iterdir()):
        support.rmdir()
    return done
