"""`refs doctor` and `refs uninstall` (doctor.py)."""

import pytest

from refs import cli, config, doctor, embed, index, keys, service

pytestmark = pytest.mark.generic_defaults


@pytest.fixture
def mac(tmp_path, monkeypatch):
    """A whole Mac in tmp_path: every path refs knows is redirected here, so
    uninstall can never reach the real service, caches or Keychain."""
    home = tmp_path / "home"
    library = home / "Dropbox"
    (library / "papers").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("REFS_DROPBOX_ROOT", str(library))
    support = home / "Library" / "Application Support" / "refs"
    monkeypatch.setenv("REFS_INDEX_DB", str(support / "index.db"))
    monkeypatch.setenv("REFS_CONFIG", str(home / ".config" / "refs" / "config.toml"))
    monkeypatch.setattr(service, "PLIST", home / "Library" / "LaunchAgents" / "refs.plist")
    monkeypatch.setattr(service, "LOG", home / "Library" / "Logs" / "refs.log")
    monkeypatch.setattr(embed, "CACHE_DIR", home / "Library" / "Caches" / "refs" / "fastembed")
    monkeypatch.setattr(service, "is_loaded", lambda: False)
    monkeypatch.setattr(service, "uninstall", lambda: service.PLIST.unlink())
    stored = {"key": "sk-ant-test"}
    monkeypatch.setattr(keys, "get_api_key", lambda: stored.get("key"))
    monkeypatch.setattr("keyring.delete_password", lambda s, a: stored.pop("key"))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    mac.home, mac.library, mac.support, mac.stored = home, library, support, stored
    return mac


def test_doctor_names_what_is_missing(mac, capsys):
    assert cli.main(["doctor", "--offline"]) == 1          # not set up: a problem
    out = capsys.readouterr().out
    assert "✗ library" in out and "refs init" in out
    assert "sk-ant" not in out                              # never the key itself


def test_doctor_is_happy_with_a_set_up_library(mac, capsys):
    config.config_path().parent.mkdir(parents=True)
    config.config_path().write_text('roots = ["papers"]\n')
    index.connect(config.load()).close()
    assert cli.main(["doctor", "--offline"]) == 0
    assert "All in order." in capsys.readouterr().out


def test_uninstall_removes_only_what_refs_made(mac, capsys):
    config.config_path().parent.mkdir(parents=True)
    config.config_path().write_text('roots = ["papers"]\n')
    cfg = config.load()
    index.connect(cfg).close()
    for path in [service.PLIST, service.LOG, embed.CACHE_DIR / "model.onnx", mac.support / "watch.lock"]:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x")
    (mac.support / "my-own-notes.txt").write_text("not refs'")          # someone else's file
    (mac.library / "papers" / "a.pdf").write_text("%PDF")
    state = cfg.absolute(cfg.state_dir)
    state.mkdir(parents=True)

    # Guard: everything uninstall would touch is inside this test's folder.
    for _, path in doctor.leftovers(cfg, keep_settings=False, keep_key=False):
        assert path is None or mac.home in path.parents

    assert cli.main(["uninstall", "--yes"]) == 0
    assert not service.PLIST.exists() and not service.LOG.exists()
    assert not (mac.home / "Library" / "Caches" / "refs").exists()
    assert not cfg.index_db.exists() and not config.config_path().exists()
    assert "key" not in mac.stored
    # Kept: your PDFs, refs' records in the library, a file refs did not make.
    assert (mac.library / "papers" / "a.pdf").exists() and state.is_dir()
    assert (mac.support / "my-own-notes.txt").exists()
    assert "uv tool uninstall refs" in capsys.readouterr().out


def test_uninstall_can_keep_settings_and_key(mac):
    config.config_path().parent.mkdir(parents=True)
    config.config_path().write_text('roots = ["papers"]\n')
    assert cli.main(["uninstall", "--yes", "--keep-settings", "--keep-key"]) == 0
    assert config.config_path().exists() and mac.stored["key"] == "sk-ant-test"


# --- the agent skill (agents.py) ----------------------------------------------------

def test_skill_is_copied_to_each_agent_present_and_links_are_left_alone(tmp_path, monkeypatch):
    from refs import agents
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".codex" / "skills").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    # Codex already has refs as a link (a developer's setup): it stays a link.
    (home / ".codex" / "skills" / "refs").symlink_to(agents.SOURCE.parent)
    lines = agents.install()
    copy = home / ".claude" / "skills" / "refs" / "SKILL.md"
    assert copy.read_bytes() == agents.SOURCE.read_bytes()
    assert (home / ".codex" / "skills" / "refs").is_symlink() and any("left as it is" in l for l in lines)
    assert not (home / ".agents").exists()                  # an agent that is not here
    copy.write_text("an older skill")
    assert ("Claude Code", copy.parent, "old") in agents.status()
    agents.install()
    assert ("Claude Code", copy.parent, "current") in agents.status()
