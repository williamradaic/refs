"""The settings page's back end: checking, saving and applying config.toml (settings.py)."""

import pymupdf
import pytest
from fastapi.testclient import TestClient

from refs import config, index, keys, settings
from refs.web.app import create_app

pytestmark = pytest.mark.generic_defaults
H = {"X-Refs": "1"}

ORIGINAL = """\
# My refs settings.

# Where my papers are.
roots = ["papers"]   # just one for now
contact_email = "jane@example.edu"
"""


@pytest.fixture
def lib(tmp_path, monkeypatch):
    root = tmp_path / "Dropbox"
    for folder in ["papers/io", "classes", "inbox"]:
        (root / folder).mkdir(parents=True)
    path = tmp_path / "config.toml"
    path.write_text(ORIGINAL)
    monkeypatch.setenv("REFS_CONFIG", str(path))
    monkeypatch.setenv("REFS_DROPBOX_ROOT", str(root))
    monkeypatch.setenv("REFS_INDEX_DB", str(tmp_path / "index.db"))
    lib.root, lib.path = root.resolve(), path
    return lib


def test_saving_changes_only_those_keys_and_keeps_comments(lib):
    cfg = settings.save({"roots": ["papers", "classes"], "auto_process": "dump"})
    text = lib.path.read_text()
    assert "# Where my papers are." in text and "# My refs settings." in text
    assert 'contact_email = "jane@example.edu"' in text
    assert cfg.roots == ["papers", "classes"] and cfg.auto_process == "dump"
    assert (lib.path.parent / "config.toml.bak").read_text() == ORIGINAL
    assert config.load().roots == ["papers", "classes"]


def test_a_new_file_starts_with_a_header(lib):
    lib.path.unlink()
    settings.save({"roots": ["papers"]})
    assert lib.path.read_text().startswith("# refs settings.")


def test_folders_from_the_dialog_are_stored_inside_the_library(lib, tmp_path):
    cfg = settings.save({"roots": [str(lib.root / "papers"), "classes/"]})
    assert cfg.roots == ["papers", "classes"]
    with pytest.raises(settings.SettingError, match="not inside the library folder") as error:
        settings.save({"roots": [str(tmp_path)]})
    assert error.value.key == "roots"
    with pytest.raises(settings.SettingError, match="no folder 'paper'"):
        settings.save({"roots": ["paper"]})          # a typo is caught, not saved
    assert config.load().roots == ["papers", "classes"]


def test_protective_lists_store_only_additions(lib):
    settings.save({"ignore_dirs": [".git", "admin", "Figures"], "exclude_paths": ["papers/visa"]})
    text = lib.path.read_text()
    assert 'ignore_dirs = ["admin"]' in text           # the defaults are not copied in
    cfg = config.load()
    assert {".git", "figures", "admin"} <= cfg.ignore_dirs
    shown = {f["key"]: f for f in settings.current(cfg)["fields"]}
    assert shown["ignore_dirs"]["value"] == ["admin"] and ".git" in shown["ignore_dirs"]["builtin"]


def test_bad_values_are_refused_and_nothing_is_written(lib):
    for change, message in [
        ({"contexts": ["Classes/{semester}"]}, "unknown placeholder"),
        ({"layout": "law"}, "must be one of"),
        ({"obsidian_vault": str(lib.root)}, "not an Obsidian vault"),
        ({"ask_monthly_cap": -1}, "0 or more"),
        ({"no_rename": ["a/b"]}, "without slashes"),
        ({"index_db": "/tmp/x"}, "cannot be changed here"),
    ]:
        with pytest.raises(settings.SettingError, match=message):
            settings.save(change)
    assert lib.path.read_text() == ORIGINAL


def test_vault_and_dump(lib, tmp_path):
    vault = tmp_path / "Notes"
    (vault / ".obsidian").mkdir(parents=True)
    cfg = settings.save({"obsidian_vault": str(vault), "dump": "to-read"})
    assert cfg.obsidian_vault == vault.resolve()
    assert (lib.root / "to-read").is_dir()               # created for you
    assert settings.save({"obsidian_vault": ""}).obsidian_vault is None


def test_rules_are_checked_against_the_whole_file(lib):
    cfg = settings.save({"contexts": ["classes/{term}/{course}"], "layout": "phd"})
    assert cfg.contexts == ["classes/{term}/{course}"] and cfg.layout == "phd"


def test_annotation_authors_found_in_pdfs(lib, tmp_path):
    pdf = lib.root / "papers" / "a.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Some text to highlight")
    for author in ["Jane Doe", "Jane Doe", "Instructor"]:
        page.add_highlight_annot(pymupdf.Rect(72, 60, 200, 80)).set_info(title=author)
    doc.save(pdf)
    cfg = config.load()
    conn = index.connect(cfg)
    conn.execute("INSERT INTO paths (path, hash) VALUES (?, ?)", ("papers/a.pdf", "h1"))
    found = {a["name"]: a for a in settings.annotation_authors(cfg, conn)}
    assert found["Jane Doe"]["marks"] == 2 and found["Jane Doe"]["files"] == 1
    assert found["Instructor"]["marks"] == 1


# --- the web API --------------------------------------------------------------------

def test_api_saves_and_uses_new_settings_at_once(lib):
    client = TestClient(create_app())
    shown = client.get("/api/settings").json()
    assert {f["key"] for f in shown["fields"]} >= {"roots", "dump", "obsidian_vault"}
    # Changing anything needs the page's own header.
    assert client.post("/api/settings", json={"changes": {"dump": "inbox"}}).status_code == 403
    answer = client.post("/api/settings", json={"changes": {"dump": "inbox"}}, headers=H)
    assert answer.status_code == 200
    assert client.get("/api/sidebar").json()["dump"] == "inbox"   # no restart needed
    bad = client.post("/api/settings", json={"changes": {"roots": ["nope"]}}, headers=H)
    assert bad.status_code == 400 and bad.json()["key"] == "roots"


def test_api_key_is_stored_never_shown(lib, monkeypatch):
    stored = []
    monkeypatch.setattr(keys, "set_api_key", stored.append)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    client = TestClient(create_app())
    assert client.post("/api/settings/key", json={"key": "hello"}, headers=H).status_code == 400
    good = "sk-ant-" + "x" * 40
    assert client.post("/api/settings/key", json={"key": good}, headers=H).status_code == 200
    assert stored == [good]
    assert good not in client.get("/api/settings").text
