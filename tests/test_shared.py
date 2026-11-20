"""Phase 2 of the research-first plan: shared collections and their copies, the
library folder, the inbox, collections on add, and marks across copies.

The same fake Dropbox as test_organize: BLP in the quick-add (library) folder,
verified, plus a shared project folder and a course folder.
"""

import json

import pymupdf
import pytest
from fastapi.testclient import TestClient

from refs import (annotations, collection, config, downloads, inbox, index, library, organize, reading,
                  rename, scan, shared, storage, works)
from refs.web.app import create_app

from test_organize import CANONICAL, write_read

H = {"X-Refs": "1"}
LIBRARY_BLP = "papers-dump/w4264.pdf"
SHARED = "phd/research/bets/readings/lit"
COURSE = "phd/coursework/2026-fall/220a/readings/03-demand-estimation-ii"


def pdf_bytes(text: str, marks: list[tuple[str, str]] = ()) -> bytes:
    """A real one-page PDF, with highlights by (author, date) if asked."""
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), text)
    for n, (author, date) in enumerate(marks):
        annot = page.add_highlight_annot(pymupdf.Rect(72, 60 + 20 * n, 200, 76 + 20 * n))
        annot.set_info(title=author, modDate=date)
        annot.update()
    data = doc.tobytes()
    doc.close()
    return data


@pytest.fixture
def lib(tmp_path, monkeypatch):
    root = tmp_path / "Dropbox"
    for folder in ("papers-dump", "phd/research/bets", COURSE):
        (root / folder).mkdir(parents=True)
    (tmp_path / "home/.Trash").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("REFS_DROPBOX_ROOT", str(root))
    monkeypatch.setenv("REFS_INDEX_DB", str(tmp_path / "index.db"))
    (root / LIBRARY_BLP).write_bytes(pdf_bytes("Automobile prices"))
    cfg = config.load()
    conn = index.connect(cfg)
    scan.scan(cfg, conn)
    file_hash = conn.execute("SELECT hash FROM paths WHERE path=?", (LIBRARY_BLP,)).fetchone()[0]
    write_read(cfg, file_hash, LIBRARY_BLP)
    scan.scan(cfg, conn)

    class Lib:
        def reload(self):
            self.cfg = config.load()
            self.conn = index.connect(self.cfg)
            return self

        def rescan(self):
            scan.scan(self.cfg, self.conn)
    lib = Lib()
    lib.root, lib.tmp, lib.hash = root, tmp_path, file_hash
    lib.reload()
    lib.work = lib.conn.execute("SELECT work_id FROM files WHERE hash=?", (file_hash,)).fetchone()[0]
    return lib


def share_bets(lib):
    collection.register(lib.cfg, "bets", kind="project", folder=SHARED, shared=True)
    return lib.reload()


# --- Settings: shared folders are watched, inbox folders are not indexed -----------------

def test_shared_folders_are_watched_and_inboxes_are_not(lib, tmp_path, monkeypatch):
    outside = "Shared with Jane/bets"
    (lib.root / outside).mkdir(parents=True)
    (lib.root / outside / "a.pdf").write_bytes(b"%PDF a")
    collection.register(lib.cfg, "bets", folder=outside, shared=True)
    (tmp_path / "config.toml").write_text('inboxes = ["phd/coursework"]\n')
    monkeypatch.setenv("REFS_CONFIG", str(tmp_path / "config.toml"))
    cfg = config.load()
    assert cfg.shared_folders == [outside] and outside in cfg.watched
    assert cfg.is_watched(f"{outside}/a.pdf") and not cfg.is_watched("elsewhere/x.pdf")
    (lib.root / COURSE / "slides.pdf").write_bytes(b"%PDF slides")
    walked = {f.rel for f in storage.walk_pdfs(cfg)}
    assert f"{outside}/a.pdf" in walked
    assert not any(rel.startswith("phd/coursework/") for rel in walked)


# --- Shared collections: copies for coauthors -----------------------------------------------

def test_adding_to_a_shared_collection_copies_the_library_file(lib):
    share_bets(lib)
    client = TestClient(create_app())
    answer = client.post("/api/collections/change", json={"work_ids": [lib.work], "name": "bets"},
                         headers=H).json()
    [copied] = answer["copied"]
    assert copied["path"].startswith(SHARED + "/") and (lib.root / copied["path"]).is_file()
    assert answer["works"][lib.work]["own_collections"] == ["bets"]
    lib.rescan()
    # Library copy + copy in the shared folder of a collection it is in: recorded,
    # so not a set of copies to review.
    assert organize.copies(lib.cfg, lib.conn) == []
    assert shared.recorded(lib.cfg, [LIBRARY_BLP, copied["path"]], ["bets"])
    assert not shared.recorded(lib.cfg, [LIBRARY_BLP, copied["path"]], [])
    # Adding again copies nothing more.
    again = client.post("/api/collections/change", json={"work_ids": [lib.work], "name": "bets"},
                        headers=H).json()
    assert again["copied"] == []
    # Taking it out deletes nothing; it lists the shared copy for the page to ask about.
    out = client.post("/api/collections/change", json={"work_ids": [lib.work], "name": "bets", "add": False},
                      headers=H).json()
    assert out["shared_copies"] == [copied["path"]] and (lib.root / copied["path"]).is_file()
    # Undo (the journal) trashes the copy, never the library file.
    assert rename.undo(lib.cfg, 1) == [(LIBRARY_BLP, copied["path"], "copy")]
    assert (lib.root / LIBRARY_BLP).is_file() and not (lib.root / copied["path"]).exists()


def test_new_shared_project_on_the_page_makes_its_folder_and_is_watched(lib):
    client = TestClient(create_app())
    answer = client.post("/api/collections/new", json={
        "name": "itau", "kind": "project", "folder": "Shared/itau/lit", "shared": True, "make_folder": True},
        headers=H).json()
    assert answer["watched"] and (lib.root / "Shared/itau/lit").is_dir()
    assert "Shared/itau/lit" in config.load().watched


# --- The library folder ---------------------------------------------------------------------

def test_place_names_canonically_moves_and_journals(lib):
    (lib.root / COURSE / "blp-draft.pdf").write_bytes(pdf_bytes("A draft"))
    lib.rescan()
    work = works.load_all(lib.cfg)[lib.work]
    # A second PDF of the same paper wants the canonical name: it gets a free one.
    new = library.place(lib.cfg, lib.conn, f"{COURSE}/blp-draft.pdf", work)
    assert new == f"papers-dump/{CANONICAL}" and (lib.root / new).is_file()
    assert not (lib.root / COURSE / "blp-draft.pdf").exists()
    journal = [json.loads(line) for line in rename.journal_path(lib.cfg).read_text().splitlines()]
    assert journal[-1]["action"] == "move" and journal[-1]["to"] == new
    with pytest.raises(library.Refused):
        library.place(lib.cfg, lib.conn, new, work)          # already in the library


def test_place_copies_out_of_a_managed_folder(lib):
    sync = "phd/coursework/2026-fall/220a/canvas-sync"
    (lib.root / sync).mkdir(parents=True)
    (lib.root / sync / "reading.pdf").write_bytes(pdf_bytes("A course reading"))
    lib.rescan()
    with pytest.raises(library.Refused, match="copy it instead"):
        library.place(lib.cfg, lib.conn, f"{sync}/reading.pdf", None)
    new = library.place(lib.cfg, lib.conn, f"{sync}/reading.pdf", None, mode="copy")
    assert new == "papers-dump/reading.pdf" and (lib.root / sync / "reading.pdf").is_file()


# --- The inbox --------------------------------------------------------------------------------

def test_inbox_offers_course_papers_with_their_course_and_takes_them(lib, tmp_path, monkeypatch):
    (tmp_path / "config.toml").write_text('inboxes = ["phd/coursework"]\n')
    monkeypatch.setenv("REFS_CONFIG", str(tmp_path / "config.toml"))
    lib.reload()
    (lib.root / COURSE / "nevo-2001.pdf").write_bytes(pdf_bytes("Measuring market power"))
    (lib.root / COURSE / "receipt.pdf").write_bytes(pdf_bytes("Amount due"))
    verdicts = {"nevo-2001.pdf": downloads.Verdict("paper", "an abstract"),
                "receipt.pdf": downloads.Verdict("other", "amount due")}
    monkeypatch.setattr(downloads, "judge", lambda path: verdicts[path.name])
    lib.rescan()                                   # the inbox is not indexed
    assert lib.conn.execute("SELECT COUNT(*) FROM paths WHERE path LIKE 'phd/coursework/%'").fetchone()[0] == 0

    assert inbox.waiting(lib.cfg, lib.conn) == (0, 2)        # nothing judged yet, nothing read
    [item] = inbox.listing(lib.cfg, lib.conn)["items"]
    assert item["name"] == "nevo-2001.pdf" and item["suggested"] == "220a/demand-estimation-ii"
    assert item["mode"] == "move" and not item["in_library"]
    assert inbox.waiting(lib.cfg, lib.conn) == (1, 0)

    result = inbox.take(lib.cfg, lib.conn, [{"path": item["path"], "collections": [item["suggested"]]}])
    [(source, new, work_id, names)] = result["taken"]
    assert new == "papers-dump/nevo-2001.pdf" and work_id is None and names == ["220a/demand-estimation-ii"]
    assert (lib.root / new).is_file() and not (lib.root / source).exists()


def test_inbox_offers_a_coauthors_pdf_from_the_shared_folder(lib):
    share_bets(lib)
    (lib.root / SHARED).mkdir(parents=True, exist_ok=True)
    (lib.root / SHARED / "new-from-jane.pdf").write_bytes(pdf_bytes("Jane's find"))
    lib.rescan()
    lib.conn.execute("UPDATE files SET kind='paper' WHERE hash IN (SELECT hash FROM paths WHERE path LIKE ?)",
                     (SHARED + "/%",))
    [item] = inbox.listing(lib.cfg, lib.conn)["items"]
    assert (item["source"], item["suggested"], item["mode"]) == ("shared", "bets", "copy")
    result = inbox.take(lib.cfg, lib.conn, [{"path": item["path"], "collections": ["bets"]}])
    [(_, new, _, _)] = result["taken"]
    assert (lib.root / new).is_file() and (lib.root / item["path"]).is_file()   # Jane keeps hers


# --- Collections on add -------------------------------------------------------------------------

def test_collect_puts_works_in_collections_and_copies_for_shared_ones(lib):
    share_bets(lib)
    assert shared.collect(lib.cfg, lib.conn, [lib.work], ["#220A", "bets", "//"]) == ["220a", "bets"]
    assert works.load_all(lib.cfg)[lib.work]["user"]["collections"] == ["220a", "bets"]
    assert any(p.startswith(SHARED + "/") for p in library.paths_of(lib.conn, lib.work))
    found = index.search(lib.conn, [], {"collection": "220a"})
    assert [f["work_id"] for f in found] == [lib.work]


def test_upload_into_the_collection_on_screen_files_a_known_pdf(lib):
    client = TestClient(create_app())
    body = (lib.root / LIBRARY_BLP).read_bytes()
    answer = client.post("/api/upload?name=blp.pdf&to=thesis", content=body,
                         headers={**H, "Content-Type": "application/pdf"}).json()
    assert "now in #thesis" in answer["message"]
    assert works.load_all(lib.cfg)[lib.work]["user"]["collections"] == ["thesis"]


# --- Marks across copies ---------------------------------------------------------------------------

def test_marks_by_author_and_flags_on_the_other_copy(lib, monkeypatch):
    lib.cfg = config.Config(**{**lib.cfg.__dict__, "annotation_authors": ["William"]})
    monkeypatch.setattr(config, "load", lambda *a, **k: lib.cfg)
    (lib.root / LIBRARY_BLP).write_bytes(pdf_bytes("Automobile prices", [("William", "D:20261112090000")]))
    (lib.root / SHARED).mkdir(parents=True)
    (lib.root / SHARED / "blp.pdf").write_bytes(
        pdf_bytes("Automobile prices", [("Jane", "D:20261118090000"), ("Jane", "D:20261117090000")]))
    lib.rescan()
    # Tie the shared copy to the same work, as a read of it would.
    lib.conn.execute("UPDATE files SET work_id=? WHERE hash IN (SELECT hash FROM paths WHERE path LIKE ?)",
                     (lib.work, SHARED + "/%"))
    found = {c["path"]: c for c in annotations.copies(lib.cfg, lib.conn, lib.work)}
    mine, theirs = found[LIBRARY_BLP], found[f"{SHARED}/blp.pdf"]
    assert mine["where"] == "library" and mine["your_pages"] == 1 and mine["flag"] is None
    assert theirs["others"] == {"Jane": 2} and theirs["latest"] == "2026-11-18T09:00:00"
    assert "Jane" in theirs["flag"]


def test_survey_reads_dates_and_authors(tmp_path):
    path = tmp_path / "x.pdf"
    path.write_bytes(pdf_bytes("x", [("Me", "D:20261101120000-08'00'"), ("", None)]))
    found = reading.survey(path, {"me"})
    assert found["by_author"] == {"Me": 1, "": 1} and found["marks"] == 1
    assert found["latest"] == "2026-11-01T12:00:00"
    assert reading.pdf_date("garbage") is None


# --- Phase 4: bringing marks over --------------------------------------------------------------------

def two_copies(lib, mine, theirs):
    """The library copy and a shared copy of the same paper, with marks."""
    import shutil
    base = lib.root / "base.pdf"
    base.write_bytes(pdf_bytes("Automobile prices"))
    for rel, marks in ((LIBRARY_BLP, mine), (f"{SHARED}/blp.pdf", theirs)):
        (lib.root / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(base, lib.root / rel)
        doc = pymupdf.open(lib.root / rel)
        page = doc[0]                           # kept: a mark comes loose from a freed page
        for n, (author, date) in enumerate(marks):
            annot = page.add_highlight_annot(pymupdf.Rect(72, 60 + 20 * n, 200, 76 + 20 * n))
            annot.set_info(title=author, modDate=date, content=f"note {author} {n}")
            annot.update()
        doc.saveIncr()
        doc.close()
    base.unlink()
    lib.rescan()
    lib.conn.execute("UPDATE files SET work_id=? WHERE hash IN (SELECT hash FROM paths WHERE path LIKE ?)",
                     (lib.work, SHARED + "/%"))
    lib.conn.commit()


def test_pull_brings_the_other_copys_marks_once(lib):
    import time
    two_copies(lib, [("William", "D:20261112090000")], [("Jane", "D:20261118090000"), ("Jane", "D:20261117090000")])
    later = time.time() + 3600
    with pytest.raises(annotations.Refused, match="last few minutes"):
        annotations.pull(lib.cfg, lib.conn, lib.work, f"{SHARED}/blp.pdf")
    result = annotations.pull(lib.cfg, lib.conn, lib.work, f"{SHARED}/blp.pdf", now=later)
    assert result["added"] == 2 and result["skipped"] == 0
    assert (lib.root / result["backup"]).is_file()
    doc = pymupdf.open(lib.root / LIBRARY_BLP)
    marks = sorted((a.info["title"], a.info["content"]) for a in doc[0].annots())
    doc.close()
    assert marks == [("Jane", "note Jane 0"), ("Jane", "note Jane 1"), ("William", "note William 0")]
    # The shared copy is untouched, and a second pull adds nothing.
    assert annotations.pull(lib.cfg, lib.conn, lib.work, f"{SHARED}/blp.pdf", now=later + 1) == \
        {"added": 0, "skipped": 0, "backup": None}


def test_pull_refuses_different_versions_and_strangers(lib):
    import time
    two_copies(lib, [], [("Jane", "D:20261118090000")])
    doc = pymupdf.open(lib.root / f"{SHARED}/blp.pdf")
    doc.new_page()
    doc.saveIncr()
    doc.close()
    later = time.time() + 3600
    with pytest.raises(annotations.Refused, match="different versions"):
        annotations.pull(lib.cfg, lib.conn, lib.work, f"{SHARED}/blp.pdf", now=later)
    with pytest.raises(annotations.Refused, match="not another copy"):
        annotations.pull(lib.cfg, lib.conn, lib.work, "phd/elsewhere.pdf", now=later)
