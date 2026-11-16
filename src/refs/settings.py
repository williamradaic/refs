"""Reading and changing config.toml from the web page.

The settings file stays the one source of truth: the page shows what it says
(and the defaults it leaves alone), and saving writes only the keys you
changed. Comments and layout in the file are kept (tomlkit edits the file in
place instead of rewriting it), and the previous version is kept next to it as
config.toml.bak.

Every change is checked before anything is written: each value on its own
(a watched folder must exist inside the library folder, a vault must be an
Obsidian vault), then the whole file, by loading it exactly as refs will.

The lists that keep files away from the app (exclude_paths, ignore_dirs,
no_llm, no_rename) only ever add to the built-in defaults, so the page shows
the defaults as fixed and stores only your additions.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import unicodedata
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path

import tomlkit

from . import config, keys

HEADER = """\
# refs settings. Only the keys set here override the defaults in
# src/refs/config.py. The settings page in refs edits this file; comments you
# write here are kept.
"""

EMBEDDING_MODELS = ["BAAI/bge-small-en-v1.5", "BAAI/bge-base-en-v1.5"]


@dataclass(frozen=True)
class Field:
    """One setting as the page shows it."""

    key: str
    group: str
    label: str
    help: str
    kind: str        # library | folders | folder | names | globs | rules | vault | text | number | choice | authors
    choices: list[str] = field(default_factory=list)
    placeholder: str = ""


FIELDS = [
    Field("dropbox_root", "Library", "Library folder", kind="library",
          help="The folder refs indexes, usually your Dropbox. Every other folder below is inside it."),
    Field("roots", "Library", "Watched folders", kind="folders",
          help="Folders scanned for PDFs, with everything inside them. New PDFs in them are read "
               "as set under AI (about $0.001 each), so adding a large folder can cost a few dollars."),
    Field("dump", "Library", "Quick-add folder", kind="folder",
          help="Where added papers and the bookmarklet's downloads go, and the one folder refs "
               "renames freely. Created if it does not exist."),
    Field("library_proxy", "Library", "Library proxy (for paywalls)", kind="text",
          placeholder="e.g. https://libproxy.berkeley.edu/login?qurl=",
          help="Your university library's proxy address. When the bookmarklet meets a paywall, it "
               "opens the page through the proxy (sign in there if asked), then click + refs again. "
               "The page's address goes after it, encoded when it ends in qurl=. Empty = none."),
    Field("exclude_paths", "Private", "Left out entirely", kind="folders",
          help="Folders or files never read, indexed or sent anywhere: visa, tax or insurance "
               "paperwork that sits inside a watched folder."),
    Field("no_llm", "Private", "Never sent to the AI", kind="folders",
          help="Indexed and searchable, but their text never goes to Anthropic (confidential "
               "manuscripts you referee, for example)."),
    Field("ignore_dirs", "Private", "Folder names skipped everywhere", kind="names",
          placeholder="e.g. admin", help="Any folder with one of these names, at any depth, is skipped."),
    Field("no_rename", "Renaming", "Folder names never renamed in", kind="names",
          placeholder="e.g. canvas-sync",
          help="Folders that another tool keeps in sync. refs never renames files inside them."),
    Field("layout", "Courses and projects", "Layout", kind="choice", choices=["folders", "phd"],
          help='"folders": each folder directly inside a watched folder is a course or project. '
               '"phd": phd/coursework/<term>/<course>, phd/research/<project>, and so on.'),
    Field("contexts", "Courses and projects", "Your own rules", kind="rules",
          placeholder="e.g. Classes/{term}/{course}",
          help="Tried before the layout. {term} = a term folder (Fall 2026), {course} = a course "
               "(Econ 240A), {name} = any folder, {area} = any folder that names the area."),
    Field("material_dirs", "Courses and projects", "Course-material folders", kind="names",
          placeholder="e.g. canvas-sync",
          help="Folder names whose PDFs are slides and handouts, not papers."),
    Field("obsidian_vault", "Notes", "Obsidian vault", kind="vault",
          help="Reading notes are Markdown files in this vault. None = notes are off."),
    Field("obsidian_folder", "Notes", "Folder in the vault", kind="text", placeholder="refs",
          help="One note per paper, named by its citekey."),
    Field("annotation_authors", "Reading status", "Annotation names that are you", kind="authors",
          help="Your highlights and notes in a PDF mark it as being read; marks by anyone else "
               "(an instructor's) do not count. Preview writes your Mac account name, iPad apps "
               "the device name."),
    Field("bib_files", "Citation keys", "Project .bib files", kind="globs",
          placeholder="e.g. ~/research/*/main.bib",
          help='refs learns the keys your projects use for its papers (read only). "~/..." is a '
               "path on this Mac, anything else is inside the library folder; ** = any depth."),
    Field("auto_process", "AI", "Read new PDFs", kind="choice", choices=["all", "dump", "none"],
          help='"all": any new PDF in a watched folder; "dump": only those in the quick-add '
               'folder; "none": leave it to `refs process`.'),
    Field("read_monthly_cap", "AI", "Monthly limit for reading ($)", kind="number",
          help="What reading new PDFs on its own may spend in a month (about $0.001 a PDF). "
               "Papers you add yourself are always read; more than 25 new PDFs at once wait "
               "for your OK on the page."),
    Field("ask", "AI", "Ask Claude when a search is unsure", kind="choice", choices=["auto", "never"],
          help="About $0.0005 a question; it sees titles, authors and summaries, never notes."),
    Field("ask_monthly_cap", "AI", "Monthly limit for asking ($)", kind="number",
          help="Once reached, searches stay local until next month."),
    Field("embedding_model", "AI", "Search model", kind="choice", choices=EMBEDDING_MODELS,
          help="Runs on this Mac. The base model is larger and somewhat better; changing it "
               "re-embeds the library at the next scan."),
    Field("contact_email", "AI", "Email for Crossref and OpenAlex", kind="text",
          placeholder="optional",
          help="They answer faster to clients that give an email. Empty = send none."),
]
BY_KEY = {f.key: f for f in FIELDS}


class SettingError(ValueError):
    """A change that cannot be saved; `key` is the setting it is about (or None)."""

    def __init__(self, key: str | None, message: str) -> None:
        super().__init__(message)
        self.key = key


# --- Showing ----------------------------------------------------------------------

def _home(path: Path | str) -> str:
    """"/Users/jane/Dropbox" -> "~/Dropbox", shorter and the same on every Mac."""
    text = str(path)
    home = str(Path.home())
    return "~" + text[len(home):] if text == home or text.startswith(home + "/") else text


def _value(cfg: config.Config, key: str):
    """A setting's current value as the page edits it."""
    value = getattr(cfg, key)
    if key in config.PROTECTIVE:           # only your additions; the defaults are fixed
        builtin = {str(v).lower() for v in config.DEFAULTS[key]}
        return [v for v in value if str(v).lower() not in builtin]
    if key == "dropbox_root":
        return _home(value)
    if key == "obsidian_vault":
        return _home(value) if value else ""
    if isinstance(value, frozenset):
        return sorted(value)
    return value


def current(cfg: config.Config | None = None, path: Path | None = None) -> dict:
    """Everything the settings page shows."""
    path = path or config.config_path()
    cfg = cfg or config.load(path)
    return {
        "file": _home(path),
        "file_exists": path.is_file(),
        "not_set_up": config.not_set_up(cfg),
        "api_key": api_key_status(),
        "vaults": obsidian_vaults(),
        "fields": [{**asdict(f), "value": _value(cfg, f.key),
                    "builtin": list(config.DEFAULTS[f.key]) if f.key in config.PROTECTIVE else []}
                   for f in FIELDS],
    }


def api_key_status() -> str:
    """"set" (in the Keychain), "environment" (ANTHROPIC_API_KEY) or "missing"."""
    import os
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "environment"
    return "set" if keys.get_api_key() else "missing"


def obsidian_vaults() -> list[str]:
    """Vaults Obsidian knows on this Mac (its own list), most recently opened first."""
    known = Path.home() / "Library" / "Application Support" / "obsidian" / "obsidian.json"
    try:
        vaults = json.loads(known.read_text()).get("vaults", {}).values()
    except (OSError, ValueError, AttributeError):
        return []
    found = sorted((v for v in vaults if isinstance(v, dict) and v.get("path")),
                   key=lambda v: -(v.get("ts") or 0))
    return [_home(v["path"]) for v in found if (Path(v["path"]) / ".obsidian").is_dir()]


# --- Checking one value -------------------------------------------------------------

def _strings(key: str, value) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise SettingError(key, "expected a list of names")
    return list(dict.fromkeys(v.strip() for v in value))


def _inside(key: str, text: str, root: Path) -> str:
    """A folder as refs stores it: relative to the library folder, NFC, no slashes
    at the ends. Accepts a full path (what the folder dialog returns) too."""
    text = unicodedata.normalize("NFC", text.strip())
    if text.startswith(("/", "~")):
        full = Path(text).expanduser().resolve()
        try:
            text = str(full.relative_to(root))
        except ValueError:
            raise SettingError(key, f"{_home(full)} is not inside the library folder ({_home(root)})") from None
    text = text.strip("/")
    if text in {"", "."} or ".." in Path(text).parts:
        raise SettingError(key, f"{text!r} is not a folder inside the library folder")
    return text


def normalize(key: str, value, root: Path):
    """Check one changed value and put it in the form config.toml stores."""
    spec = BY_KEY.get(key)
    if spec is None:
        raise SettingError(key, f"{key} cannot be changed here")
    kind = spec.kind

    if kind == "library":
        folder = Path(str(value).strip()).expanduser()
        if not folder.is_dir():
            raise SettingError(key, f"there is no folder {value!r}")
        return _home(folder.resolve())

    if kind == "folders":                        # (save() checks that watched folders exist)
        folders = [_inside(key, v, root) for v in _strings(key, value) if v]
        return _without_builtin(key, folders)

    if kind == "folder":
        return _inside(key, str(value), root)

    if kind in {"names", "globs", "rules"}:
        names = [v for v in _strings(key, value) if v]
        if kind == "names" and any("/" in name for name in names):
            raise SettingError(key, "folder names only, without slashes")
        return _without_builtin(key, names)

    if kind == "authors":
        return _strings(key, value)              # "" is allowed: marks with no author

    if kind == "vault":
        text = str(value).strip()
        if not text:
            return ""                            # notes off
        folder = Path(text).expanduser()
        if not (folder / ".obsidian").is_dir():
            raise SettingError(key, f"{text} is not an Obsidian vault (it has no .obsidian folder)")
        return _home(folder.resolve())

    if kind == "number":
        try:
            number = float(value)
        except (TypeError, ValueError):
            raise SettingError(key, "expected a number") from None
        if number < 0:
            raise SettingError(key, "expected a number of 0 or more")
        return number

    if kind == "choice":
        if value not in spec.choices:
            raise SettingError(key, f"must be one of {', '.join(spec.choices)}")
        return value

    text = str(value).strip()                    # kind == "text"
    if key == "obsidian_folder":
        text = text.strip("/") or "refs"
    if key == "library_proxy" and text and not text.lower().startswith("https://"):
        raise SettingError(key, "a proxy address starts with https://")
    if key == "contact_email" and text and "@" not in text:
        raise SettingError(key, f"{text!r} is not an email address")
    return text


def _without_builtin(key: str, values: list[str]) -> list[str]:
    """The protective lists store only additions to their defaults."""
    if key not in config.PROTECTIVE:
        return values
    builtin = {str(v).lower() for v in config.DEFAULTS[key]}
    return [v for v in values if v.lower() not in builtin]


# --- Saving -------------------------------------------------------------------------

def _toml(value):
    """A value as tomlkit writes it: lists one item per line, like the file's own."""
    if isinstance(value, list):
        array = tomlkit.array()
        array.extend(value)
        return array.multiline(len(value) > 1)
    return value


def save(changes: dict, path: Path | None = None) -> config.Config:
    """Check `changes` ({key: new value}), write them into config.toml and
    return the settings as refs will now load them. Raises SettingError, and
    writes nothing, if any change cannot work."""
    path = path or config.config_path()
    before = config.load(path)

    # The library folder first: the other folders are checked against it.
    clean: dict = {}
    if "dropbox_root" in changes:
        clean["dropbox_root"] = normalize("dropbox_root", changes["dropbox_root"], before.dropbox_root)
    root = Path(clean["dropbox_root"]).expanduser() if "dropbox_root" in clean else before.dropbox_root
    for key, value in changes.items():
        if key != "dropbox_root":
            clean[key] = normalize(key, value, root)

    # The quick-add folder is always watched: a paper saved there must be found.
    # (It is created below if new, so it need not exist yet.)
    roots = clean.get("roots", before.roots)
    dump = clean.get("dump", before.dump)
    if roots and not any(dump == r or dump.startswith(r + "/") for r in roots):
        clean["roots"] = roots = [*roots, dump]
    if "roots" in clean or "dropbox_root" in clean:
        for folder in roots:
            if folder != dump and not (root / folder).is_dir():
                raise SettingError("roots", f"there is no folder {folder!r} in the library folder")

    text = path.read_text(encoding="utf-8") if path.is_file() else HEADER
    try:
        document = tomlkit.parse(text)
    except tomlkit.exceptions.TOMLKitError as error:
        raise SettingError(None, f"{_home(path)} cannot be read ({error}); fix it by hand first") from None
    for key, value in clean.items():
        document[key] = _toml(value)
    new_text = tomlkit.dumps(document)

    # The whole file, loaded exactly as refs will load it (layout names, rules).
    check = path.with_name(f".{path.name}.check")
    check.parent.mkdir(parents=True, exist_ok=True)
    check.write_text(new_text, encoding="utf-8")
    try:
        after = config.load(check)
    except SystemExit as error:
        raise SettingError(None, str(error).removeprefix("config.toml: ")) from None
    finally:
        check.unlink(missing_ok=True)

    # Keep the previous version, then swap the new file in (never half-written).
    if path.is_file():
        shutil.copy2(path, path.with_name(path.name + ".bak"))
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(new_text, encoding="utf-8")
    temporary.replace(path)

    # A new quick-add folder is created, so adding a paper never fails on it.
    if "dump" in clean:
        after.absolute(after.dump).mkdir(parents=True, exist_ok=True)
    return after


# --- Helpers for the page -----------------------------------------------------------

def _applescript(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def choose_folder(prompt: str, start: Path | None = None) -> Path | None:
    """The macOS folder dialog. The chosen folder, or None if cancelled."""
    ask = f"choose folder with prompt {_applescript(prompt)}"
    if start is not None and start.is_dir():
        ask += f" default location (POSIX file {_applescript(str(start))})"
    # `activate` brings the dialog in front of the browser window.
    done = subprocess.run(["osascript", "-e", "activate", "-e", f"POSIX path of ({ask})"],
                          capture_output=True, text=True, timeout=900)
    chosen = done.stdout.strip()
    return Path(chosen) if done.returncode == 0 and chosen else None


def annotation_authors(cfg: config.Config, conn) -> list[dict]:
    """Every author name on highlights and notes in the library's PDFs, with
    how many files and marks carry it, most files first. About 4 ms a file."""
    from . import reading

    files: Counter[str] = Counter()
    marks: Counter[str] = Counter()
    rows = conn.execute("SELECT MIN(path) AS path FROM paths GROUP BY hash").fetchall()
    for row in rows:
        found = reading.marks_by_author(cfg.absolute(row["path"]))
        for name, count in found.items():
            files[name] += 1
            marks[name] += count
    mine = {name.strip().lower() for name in cfg.annotation_authors}
    return [{"name": name, "files": n, "marks": marks[name], "mine": name.lower() in mine}
            for name, n in files.most_common()]
