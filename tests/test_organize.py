"""Review actions through the web API: renames, copies, Trash, File to…, Copy to…, and .bib appending.

Runs against a fake Dropbox (and a fake home folder, so "the Trash" is a
temporary folder too). Works are given read records directly, as in test_works.
"""

import json

import pytest
from fastapi.testclient import TestClient

from refs import bib, config, index, scan
from refs.web.app import create_app

H = {"X-Refs": "1"}
DUMP_NAME = "papers-dump/w4264.pdf"
CANONICAL = "berry-levinsohn-pakes-1995-automobile-prices-in-market-equilibrium.pdf"
READINGS = "phd/coursework/2026-fall/220a/readings/03-demand-estimation-ii"


def write_read(cfg, file_hash, rel):
    """A verified BLP read record for one file."""
    title = "Automobile Prices in Market Equilibrium"
    authors = [{"given": "S.", "family": f} for f in ("Berry", "Levinsohn", "Pakes")]
    read = {"hash": file_hash, "path": rel, "status": "verified", "issues": [],
            "reading": {"kind": "paper", "title": title, "year": 1995, "venue": "Econometrica",
                        "authors": authors, "is_working_paper": False, "description": "",
                        "fields": [], "methods": [], "topics": []},
            "registry": {"via": "doi", "record": {
                "title": title, "year": 1995, "venue": "Econometrica", "doi": "10.2307/2171802",
                "type": "journal-article", "authors": authors, "volume": "63", "issue": "4",
                "pages": "841-890", "publisher": "JSTOR", "source": "crossref"}}}
    path = cfg.absolute(cfg.state_dir) / "reads" / f"{file_hash}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(read))


@pytest.fixture
def lib(tmp_path, monkeypatch):
    root = tmp_path / "Dropbox"
    (root / "papers-dump").mkdir(parents=True)
    (root / READINGS).mkdir(parents=True)
    (root / "phd/research/bets/overleaf").mkdir(parents=True)
    (tmp_path / "home/.Trash").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("REFS_DROPBOX_ROOT", str(root))
    monkeypatch.setenv("REFS_INDEX_DB", str(tmp_path / "index.db"))
    monkeypatch.setenv("REFS_CONFIG", str(tmp_path / "none.toml"))
    (root / DUMP_NAME).write_bytes(b"%PDF blp")
    (root / READINGS / "blp.pdf").write_bytes(b"%PDF blp")       # an identical copy
    cfg = config.load()
    conn = index.connect(cfg)
    scan.scan(cfg, conn)
    file_hash = conn.execute("SELECT hash FROM paths WHERE path=?", (DUMP_NAME,)).fetchone()[0]
    write_read(cfg, file_hash, DUMP_NAME)
    scan.scan(cfg, conn)                 # files it under a work

    class Lib:
        pass
    lib = Lib()
    lib.root, lib.cfg, lib.hash, lib.home = root, cfg, file_hash, tmp_path / "home"
    lib.client = TestClient(create_app())
    return lib


def test_renames_are_proposed_applied_and_undone(lib):
    proposals = lib.client.get("/api/renames").json()["proposals"]
    assert {p["path"] for p in proposals} == {DUMP_NAME, f"{READINGS}/blp.pdf"}
    # Only the ticked file is renamed.
    done = lib.client.post("/api/renames/apply", json={"paths": [DUMP_NAME]}, headers=H).json()
    assert done == {"done": 1}
    assert (lib.root / "papers-dump" / CANONICAL).exists()
    assert (lib.root / READINGS / "blp.pdf").exists()
    # A path refs did not propose cannot be renamed through the page.
    assert lib.client.post("/api/renames/apply", json={"paths": ["phd/x.pdf"]}, headers=H).json() == {"done": 0}
    assert lib.client.post("/api/undo", json={"count": 1}, headers=H).json() == {"restored": 1}
    assert (lib.root / DUMP_NAME).exists()


def test_state_changes_need_the_page_header(lib):
    assert lib.client.post("/api/renames/apply", json={"paths": [DUMP_NAME]}).status_code == 403
    assert lib.client.post("/api/trash", json={"path": DUMP_NAME}).status_code == 403


def test_copies_trash_and_keep(lib):
    [item] = lib.client.get("/api/copies").json()["items"]
    assert len(item["locations"]) == 2
    # One copy goes to the Trash; the last one is refused.
    assert lib.client.post("/api/trash", json={"path": DUMP_NAME}, headers=H).status_code == 200
    assert (lib.home / ".Trash/w4264.pdf").exists() and not (lib.root / DUMP_NAME).exists()
    refused = lib.client.post("/api/trash", json={"path": f"{READINGS}/blp.pdf"}, headers=H)
    assert refused.status_code == 409 and "only copy" in refused.json()["detail"]
    assert lib.client.get("/api/copies").json()["items"] == []


def test_keep_takes_a_set_out_of_review_and_unkeep_brings_it_back(lib):
    assert lib.client.post("/api/copies/keep", json={"hash": lib.hash}, headers=H).status_code == 200
    found = lib.client.get("/api/copies").json()
    assert found["items"] == [] and [k["hash"] for k in found["kept"]] == [lib.hash]
    assert lib.client.post("/api/copies/unkeep", json={"hash": lib.hash}).status_code == 403
    assert lib.client.post("/api/copies/unkeep", json={"hash": lib.hash}, headers=H).status_code == 200
    found = lib.client.get("/api/copies").json()
    assert [i["hash"] for i in found["items"]] == [lib.hash] and found["kept"] == []


def test_file_to_moves_into_a_known_folder_and_undoes(lib):
    folders = lib.client.get("/api/folders").json()["folders"]
    assert READINGS in folders and not any(f.startswith("papers-dump") for f in folders)
    (lib.root / READINGS / "blp.pdf").unlink()       # make room: same name would be refused
    scan.scan(lib.cfg, index.connect(lib.cfg))
    moved = lib.client.post("/api/move", json={"path": DUMP_NAME, "folder": READINGS}, headers=H)
    assert moved.json() == {"path": f"{READINGS}/w4264.pdf"}
    assert (lib.root / READINGS / "w4264.pdf").exists()
    # Never into a managed folder, never outside the scanned roots.
    for folder in ("phd/research/bets/overleaf", "../elsewhere"):
        r = lib.client.post("/api/move", json={"path": f"{READINGS}/w4264.pdf", "folder": folder}, headers=H)
        assert r.status_code == 409
    lib.client.post("/api/undo", json={"count": 1}, headers=H)
    assert (lib.root / DUMP_NAME).exists()


def test_copy_to_leaves_the_original_and_undo_trashes_the_copy(lib):
    (lib.root / READINGS / "blp.pdf").unlink()
    scan.scan(lib.cfg, index.connect(lib.cfg))
    copied = lib.client.post("/api/copy", json={"path": DUMP_NAME, "folder": READINGS}, headers=H)
    assert copied.json() == {"path": f"{READINGS}/w4264.pdf"}
    assert (lib.root / READINGS / "w4264.pdf").exists() and (lib.root / DUMP_NAME).exists()
    # The deliberate copy stays out of the Review queue.
    assert lib.client.get("/api/copies").json()["items"] == []
    # Same name already there, or a managed folder: refused.
    again = lib.client.post("/api/copy", json={"path": DUMP_NAME, "folder": READINGS}, headers=H)
    assert again.status_code == 409
    r = lib.client.post("/api/copy", json={"path": DUMP_NAME, "folder": "phd/research/bets/overleaf"}, headers=H)
    assert r.status_code == 409
    assert lib.client.post("/api/undo", json={"count": 1}, headers=H).json() == {"restored": 1}
    assert not (lib.root / READINGS / "w4264.pdf").exists()
    assert (lib.home / ".Trash/w4264.pdf").exists() and (lib.root / DUMP_NAME).exists()


def test_undo_never_trashes_a_copy_whose_original_is_gone(lib):
    (lib.root / READINGS / "blp.pdf").unlink()
    scan.scan(lib.cfg, index.connect(lib.cfg))
    lib.client.post("/api/copy", json={"path": DUMP_NAME, "folder": READINGS}, headers=H)
    (lib.root / DUMP_NAME).unlink()                  # the copy is now the only one
    assert lib.client.post("/api/undo", json={"count": 1}, headers=H).json() == {"restored": 0}
    assert (lib.root / READINGS / "w4264.pdf").exists()


def test_bookmarklet_page_is_served(lib):
    page = lib.client.get("/bookmarklet")
    assert page.status_code == 200 and "?add=" in page.text


def test_bib_append_skips_papers_already_there_under_another_key(tmp_path):
    work = {"citekey": "berry1995automobile", "meta": {
        "kind": "paper", "type": "journal-article", "title": "Automobile Prices in Market Equilibrium",
        "year": 1995, "venue": "Econometrica", "doi": "10.2307/2171802",
        "authors": [{"given": "Steven", "family": "Berry"}]}}
    other = {"citekey": "nevo2001measuring", "meta": {
        "kind": "paper", "type": "journal-article", "title": "Measuring Market Power",
        "year": 2001, "venue": "Econometrica", "doi": None, "authors": [{"family": "Nevo"}]}}
    target = tmp_path / "references.bib"
    target.write_text("@string{ecta = {Econometrica}}\n"
                      "@article{BLP95,\n  title = {{Automobile} prices in market equilibrium},\n}")
    added, present = bib.append(target, [work, other])
    assert [w["citekey"] for w in added] == ["nevo2001measuring"]
    assert present == [(work, "BLP95")]
    text = target.read_text()
    assert text.count("@article{nevo2001measuring,") == 1 and "}\n\n@article{nevo" in text
    # Running it again adds nothing.
    assert bib.append(target, [work, other])[0] == []
