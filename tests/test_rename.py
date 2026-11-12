"""Renaming: canonical names, protected folders, journal and undo."""

import pytest

from refs import config, rename

WORK = {"meta": {"status": "verified", "year": 1998, "title": "A model of investor sentiment",
                 "authors": [{"family": "Barberis"}, {"family": "Shleifer"}, {"family": "Vishny"}]}}


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    root = tmp_path / "Dropbox"
    (root / "papers-dump").mkdir(parents=True)
    monkeypatch.setenv("REFS_DROPBOX_ROOT", str(root))
    monkeypatch.setenv("REFS_INDEX_DB", str(tmp_path / "index.db"))
    monkeypatch.setenv("REFS_CONFIG", str(tmp_path / "none.toml"))
    return config.load()


def test_proposed_name_only_for_verified_works():
    assert rename.proposed_name(WORK, "1-s2.0-S0304405X98000270-main.pdf") == \
        "barberis-shleifer-vishny-1998-model-of-investor-sentiment.pdf"
    assert rename.proposed_name(WORK, "scan-ocr.pdf").endswith("-ocr.pdf")
    unverified = {"meta": {**WORK["meta"], "status": "extracted"}}
    assert rename.proposed_name(unverified, "x.pdf") is None


def test_rename_journal_and_undo(cfg):
    (cfg.dropbox_root / "papers-dump/junk.pdf").write_bytes(b"x")
    new = rename.apply(cfg, "papers-dump/junk.pdf", "barberis-1998-model.pdf", "abc")
    assert new == "papers-dump/barberis-1998-model.pdf"
    assert (cfg.dropbox_root / new).exists() and not (cfg.dropbox_root / "papers-dump/junk.pdf").exists()

    assert rename.undo(cfg) == [("papers-dump/junk.pdf", "papers-dump/barberis-1998-model.pdf")]
    assert (cfg.dropbox_root / "papers-dump/junk.pdf").exists()
    assert rename.undo(cfg) == []                     # journal is now empty


def test_never_overwrites_and_respects_protected_folders(cfg):
    dump = cfg.dropbox_root / "papers-dump"
    (dump / "a.pdf").write_bytes(b"a")
    (dump / "taken.pdf").write_bytes(b"other")
    assert rename.apply(cfg, "papers-dump/a.pdf", "taken.pdf") is None
    assert (dump / "taken.pdf").read_bytes() == b"other"

    managed = cfg.dropbox_root / "phd/coursework/2026-fall/220a/canvas-sync/slides"
    managed.mkdir(parents=True)
    (managed / "w1.pdf").write_bytes(b"s")
    rel = "phd/coursework/2026-fall/220a/canvas-sync/slides/w1.pdf"
    assert "managed folder" in rename.blocked_reason(cfg, rel)
    assert rename.apply(cfg, rel, "new.pdf") is None and (managed / "w1.pdf").exists()

    repo = cfg.dropbox_root / "phd/research/proj"
    (repo / ".git").mkdir(parents=True)
    (repo / "readings").mkdir()
    (repo / "readings/p.pdf").write_bytes(b"p")
    assert rename.blocked_reason(cfg, "phd/research/proj/readings/p.pdf") == "inside a Git repository"


def test_disambiguate_versions_of_the_same_paper():
    name = "allcott-gentzkow-song-2022-digital-addiction.pdf"
    assert rename.disambiguate(name, set(), False) == name
    assert rename.disambiguate(name, {name}, True) == name.replace(".pdf", "-wp.pdf")
    assert rename.disambiguate(name, {name}, False) == name.replace(".pdf", "-2.pdf")
    both = {name, name.replace(".pdf", "-wp.pdf")}
    assert rename.disambiguate(name, both, True) == name.replace(".pdf", "-2.pdf")
