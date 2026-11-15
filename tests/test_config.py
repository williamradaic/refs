"""The shipped defaults are generic, and refs copes with what they leave unset."""

import pytest

from refs import config, notes
from refs.watch import Watcher

pytestmark = pytest.mark.generic_defaults


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("REFS_DROPBOX_ROOT", str(tmp_path / "Dropbox"))
    monkeypatch.setenv("REFS_INDEX_DB", str(tmp_path / "index.db"))
    (tmp_path / "Dropbox").mkdir()
    return config.load()


def test_defaults_name_no_one_s_folders(cfg):
    # Nothing in the defaults may assume one person's Dropbox or vault.
    # (annotation_authors is this Mac account's own name, so it is left out.)
    text = repr({k: v for k, v in config.DEFAULTS.items() if k != "annotation_authors"}).lower()
    for personal in ["phd", "wr-vault", "william", "canvas-sync", "referee", "admission"]:
        assert personal not in text
    assert cfg.roots == [] and cfg.state_dir == ".refs"
    assert cfg.obsidian_vault is None and cfg.bib_files == []


def test_without_folders_refs_says_it_is_not_set_up(cfg):
    assert "roots" in config.not_set_up(cfg)


def test_with_a_folder_it_is_set_up(tmp_path, monkeypatch, cfg):
    settings = tmp_path / "config.toml"
    settings.write_text('roots = ["papers"]\n')
    monkeypatch.setenv("REFS_CONFIG", str(settings))
    assert config.not_set_up(config.load()) is None


def test_annotation_authors_default_to_this_mac_account(cfg, monkeypatch):
    # Preview writes the account's full name; "" counts marks with no author.
    assert cfg.annotation_authors[-1] == ""
    monkeypatch.setattr(config, "_macos_full_name", lambda: "Jane Doe")
    assert [n for n in [config._macos_full_name()] if n] + [""] == ["Jane Doe", ""]


def test_no_vault_means_notes_are_off(cfg):
    work = {"id": "w1", "citekey": "doe2020x", "meta": {}}
    assert not notes.enabled(cfg) and notes.folder(cfg) is None
    assert notes.read(cfg, work)["available"] is False
    assert notes.bodies(cfg) == {}
    with pytest.raises(FileNotFoundError, match="notes are off"):
        notes.write(cfg, work, "text", "")
    # The watcher must not treat the current folder as the notes folder.
    watcher = Watcher(cfg, notifier=None)
    assert not watcher.relevant(str(cfg.dropbox_root / "a.md"), False)


def test_an_empty_vault_setting_is_not_the_current_folder(tmp_path, monkeypatch, cfg):
    settings = tmp_path / "config.toml"
    settings.write_text('obsidian_vault = ""\n')
    monkeypatch.setenv("REFS_CONFIG", str(settings))
    assert config.load().obsidian_vault is None


def test_layout_and_rules_are_checked_when_loading(tmp_path, monkeypatch, cfg):
    assert (cfg.layout, cfg.contexts, cfg.material_dirs) == ("folders", [], [])
    settings = tmp_path / "config.toml"
    monkeypatch.setenv("REFS_CONFIG", str(settings))
    settings.write_text('layout = "phd"\ncontexts = ["Classes/{term}/{course}"]\n')
    assert config.load().contexts == ["Classes/{term}/{course}"]
    settings.write_text('contexts = ["Classes/{semester}"]\n')
    with pytest.raises(SystemExit, match="unknown placeholder"):
        config.load()
    settings.write_text('layout = "law-school"\n')
    with pytest.raises(SystemExit, match="layout must be one of folders, phd"):
        config.load()
