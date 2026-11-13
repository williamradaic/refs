"""Reading notes in the Obsidian vault (notes.py)."""

import dataclasses
from pathlib import Path

import pytest

from refs import notes
from refs.config import Config

WORK = {
    "id": "wabc123", "citekey": "odonoghue2006optimal",
    "meta": {"title": "Optimal Sin Taxes", "year": 2006, "doi": "10.1/x",
             "authors": [{"given": "Ted", "family": "O'Donoghue"},
                         {"given": "Matthew", "family": "Rabin"}]},
}


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    vault = tmp_path / "vault"
    (vault / ".obsidian").mkdir(parents=True)
    base = Config(dropbox_root=tmp_path, index_db=tmp_path / "index.db")
    return dataclasses.replace(base, obsidian_vault=vault, obsidian_folder="phd/refs")


def test_missing_note_reads_empty_and_is_not_created_empty(cfg):
    note = notes.read(cfg, WORK)
    assert note == note | {"body": "", "version": "", "exists": False, "available": True}
    notes.write(cfg, WORK, "  \n", "")
    assert not notes.path_for(cfg, WORK).exists()


def test_first_save_writes_properties_then_keeps_them(cfg):
    first = notes.write(cfg, WORK, "Key idea: sin taxes correct self-control.", "")
    text = notes.path_for(cfg, WORK).read_text()
    assert text.startswith("---\ntitle: \"Optimal Sin Taxes\"\nauthors:\n  - \"Ted O'Donoghue\"")
    assert "citekey: odonoghue2006optimal\nrefs: wabc123\n" in text
    assert "\"O'Donoghue and Rabin (2006)\"" in text
    assert first["body"] == "Key idea: sin taxes correct self-control.\n"

    # A property added in Obsidian survives the next save from the page.
    path = notes.path_for(cfg, WORK)
    path.write_text(text.replace("refs: wabc123\n", "refs: wabc123\nstatus: done\n"))
    version = notes.read(cfg, WORK)["version"]
    notes.write(cfg, WORK, "Rewritten.", version)
    text = path.read_text()
    assert "status: done\n" in text and text.endswith("---\n\nRewritten.\n")


def test_save_refused_if_file_changed_since_it_was_read(cfg):
    version = notes.write(cfg, WORK, "From the page.", "")["version"]
    path = notes.path_for(cfg, WORK)
    path.write_text(path.read_text() + "Added in Obsidian.\n")
    with pytest.raises(notes.Conflict):
        notes.write(cfg, WORK, "From the page, again.", version)
    assert "Added in Obsidian." in path.read_text()


def test_no_vault_means_no_write(cfg, tmp_path):
    cfg = dataclasses.replace(cfg, obsidian_vault=tmp_path / "nowhere")
    assert notes.read(cfg, WORK)["available"] is False
    with pytest.raises(FileNotFoundError):
        notes.write(cfg, WORK, "text", "")
