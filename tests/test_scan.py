"""The scanner's identity rules, exercised on a small fake Dropbox folder.

Each test builds a temporary tree, scans it, changes something the way Finder
or Dropbox would, scans again, and checks what the scanner concluded.
"""

import os
import shutil
from pathlib import Path

import pytest

from refs import config, index, scan

READING = "phd/coursework/2026-fall/220a/readings/03-demand-estimation-ii"
BLP = f"{READING}/berry-levinsohn-pakes-1995-automobile-prices-in-market-equilibrium.pdf"


@pytest.fixture
def lib(tmp_path, monkeypatch):
    """A fake Dropbox root with its own throwaway index."""
    root = tmp_path / "Dropbox"
    monkeypatch.setenv("REFS_DROPBOX_ROOT", str(root))
    monkeypatch.setenv("REFS_INDEX_DB", str(tmp_path / "index.db"))
    monkeypatch.setenv("REFS_CONFIG", str(tmp_path / "no-such-config.toml"))
    (root / "phd").mkdir(parents=True)
    (root / "papers-dump").mkdir()

    class Lib:
        def __init__(self):
            self.root = root
            self.cfg = config.load()
            self.conn = index.connect(self.cfg)

        def write(self, rel: str, content: bytes) -> Path:
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
            return path

        def scan(self) -> scan.ScanReport:
            return scan.scan(self.cfg, self.conn)

        def paths(self) -> set[str]:
            return {r["path"] for r in self.conn.execute("SELECT path FROM paths")}

    return Lib()


def test_first_scan_adds_and_second_scan_is_quiet(lib):
    lib.write(BLP, b"blp content")
    lib.write("papers-dump/w32515.pdf", b"nber paper")
    lib.write("phd/notes.txt", b"not a pdf")

    first = lib.scan()
    assert sorted(first.added) == sorted([BLP, "papers-dump/w32515.pdf"])

    second = lib.scan()
    assert second.changes == 0 and second.unchanged == 2


def test_rename_and_move_keep_the_same_file(lib):
    lib.write("papers-dump/w32515.pdf", b"nber paper")
    lib.scan()
    before = lib.conn.execute("SELECT hash FROM paths").fetchone()["hash"]

    # Rename in place, then move to a course folder: one file throughout.
    target = lib.root / READING / "someone-2024-a-paper.pdf"
    target.parent.mkdir(parents=True)
    os.rename(lib.root / "papers-dump/w32515.pdf", target)

    report = lib.scan()
    assert report.moved == [("papers-dump/w32515.pdf", f"{READING}/someone-2024-a-paper.pdf")]
    assert report.added == [] and report.removed == []
    row = lib.conn.execute("SELECT hash, context, topic FROM paths").fetchone()
    assert row["hash"] == before
    assert (row["context"], row["topic"]) == ("coursework/2026-fall/220a", "Demand estimation II")
    assert lib.conn.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 1


def test_move_is_recognised_by_content_when_the_inode_changes(lib):
    # Dropbox can replace a file rather than rename it: new inode, same bytes.
    lib.write("papers-dump/a.pdf", b"same bytes")
    lib.scan()
    (lib.root / "papers-dump/a.pdf").unlink()
    lib.write("phd/research/bets/readings/lit/a.pdf", b"same bytes")

    report = lib.scan()
    assert report.moved == [("papers-dump/a.pdf", "phd/research/bets/readings/lit/a.pdf")]


def test_copy_is_one_file_in_two_places(lib):
    original = lib.write(BLP, b"blp content")
    lib.scan()
    copy_rel = "phd/coursework/2026-fall/244/readings/08-discrete-choice/blp.pdf"
    (lib.root / copy_rel).parent.mkdir(parents=True)
    shutil.copy2(original, lib.root / copy_rel)

    report = lib.scan()
    assert report.copies == [copy_rel] and report.added == []
    assert lib.conn.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 1
    found = index.search(lib.conn, ["blp"])
    assert len(found) == 1 and len(found[0]["locations"]) == 2


def test_edit_in_place_links_new_version_to_old(lib):
    path = lib.write(BLP, b"blp content")
    lib.scan()
    old_hash = lib.conn.execute("SELECT hash FROM paths").fetchone()["hash"]

    path.write_bytes(b"blp content, now annotated")     # e.g. highlights added
    report = lib.scan()

    assert report.modified == [BLP]
    new = lib.conn.execute(
        "SELECT f.hash, f.supersedes FROM paths p JOIN files f ON f.hash = p.hash"
    ).fetchone()
    assert new["hash"] != old_hash and new["supersedes"] == old_hash


def test_delete_then_restore(lib):
    path = lib.write(BLP, b"blp content")
    lib.scan()
    path.unlink()

    report = lib.scan()
    assert report.removed == [BLP] and lib.paths() == set()
    # The content is remembered even with no location left.
    assert lib.conn.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 1

    lib.write("papers-dump/blp-again.pdf", b"blp content")
    report = lib.scan()
    assert report.restored == ["papers-dump/blp-again.pdf"] and report.added == []


def test_ignored_folders_symlinks_and_state_dir_are_skipped(lib):
    lib.write(BLP, b"blp content")
    lib.write("phd/research/bets/paper/fig/plot.pdf", b"a figure")
    lib.write("phd/coursework/2026-fall/220a/canvas-sync/_versions/old.pdf", b"old")
    lib.write("phd/tools/refs/works/whatever.pdf", b"app state")
    # A symlinked folder pointing back into the tree must not double-count.
    os.symlink(lib.root / "phd/coursework", lib.root / "phd/shortcut")

    lib.scan()
    assert lib.paths() == {BLP}


def test_search_by_text_initials_and_filters(lib):
    lib.write(BLP, b"blp content")
    lib.write(f"{READING}/nevo-2001-measuring-market-power-cereal-industry.pdf", b"nevo")
    lib.write("phd/coursework/2026-fall/220a/canvas-sync/slides/Week 4 Fall 2026.pdf", b"slides")
    lib.write("phd/research/bets/readings/lit/becker-murphy-1988-a-theory-of-rational-addiction.pdf", b"bm")
    lib.scan()

    titles = lambda results: [r["title"] for r in results]   # noqa: E731

    assert titles(index.search(lib.conn, ["blp"])) == ["automobile prices in market equilibrium"]
    assert titles(index.search(lib.conn, ["cereal"])) == ["measuring market power cereal industry"]
    assert len(index.search(lib.conn, [], {"course": "220a"})) == 3
    assert len(index.search(lib.conn, [], {"course": "220a", "kind": "paper"})) == 2
    assert titles(index.search(lib.conn, [], {"project": "bets"})) == ["a theory of rational addiction"]
    assert titles(index.search(lib.conn, [], {"topic": "demand", "year": "2001"})) == [
        "measuring market power cereal industry"]
    with pytest.raises(ValueError):
        index.search(lib.conn, [], {"nonsense": "x"})


def test_personal_document_folders_are_never_indexed(lib):
    lib.write("phd/admission/Visit Day/passport-scan.pdf", b"private")
    lib.write("phd/move-in/insurance-letter.pdf", b"private")
    lib.write(BLP, b"blp content")
    lib.scan()
    assert lib.paths() == {BLP}


def test_processed_files_are_searchable_by_registry_metadata(lib):
    import hashlib
    import json

    # A badly named file, plus the read record the pipeline would have written.
    lib.write("papers-dump/1-s2.0-junk-main.pdf", b"bsv content")
    file_hash = hashlib.sha256(b"bsv content").hexdigest()
    reads = lib.root / "phd/tools/refs/reads"
    reads.mkdir(parents=True)
    (reads / f"{file_hash}.json").write_text(json.dumps({
        "status": "verified", "issues": [],
        "reading": {"kind": "paper", "title": "A model of investor sentiment", "year": 1998,
                    "authors": [], "venue": None, "description": "Investors over- and underreact.",
                    "fields": ["finance"], "methods": ["theory"], "topics": ["investor sentiment"]},
        "registry": {"via": "doi", "record": {
            "title": "A model of investor sentiment", "year": 1998,
            "venue": "Journal of Financial Economics", "doi": "10.1016/s0304-405x(98)00027-0",
            "source": "crossref",
            "authors": [{"given": "Nicholas", "family": "Barberis"},
                        {"given": "Andrei", "family": "Shleifer"},
                        {"given": "Robert", "family": "Vishny"}]}},
    }))
    lib.scan()

    for query in (["investor", "sentiment"], ["shleifer"], ["bsv"], ["financial", "economics"], ["underreact"]):
        found = index.search(lib.conn, query)
        assert len(found) == 1, query
    item = index.search(lib.conn, [], {"tag": "finance", "status": "verified", "year": "1998"})[0]
    assert item["authors"] == ["Barberis", "Shleifer", "Vishny"]
    assert item["kind"] == "paper" and item["doi"].startswith("10.1016/")
    assert index.search(lib.conn, [], {"status": "unprocessed"}) == []


def test_admin_folders_and_excluded_files_are_never_read(lib, tmp_path, monkeypatch):
    # Paperwork in any folder named "admin" is skipped, at any depth.
    lib.write("phd/admin/gsr-contract.pdf", b"contract")
    lib.write("phd/research/bets/local/Admin/reimbursement.pdf", b"receipt")
    lib.write(f"{READING}/transcript.pdf", b"grades")
    lib.write(BLP, b"blp content")

    # One more file excluded in config.toml; the default exclusions still hold.
    settings = tmp_path / "config.toml"
    settings.write_text(f'exclude_paths = ["{READING}/transcript.pdf"]\n')
    monkeypatch.setenv("REFS_CONFIG", str(settings))
    lib.cfg = config.load()
    assert "phd/admission" in lib.cfg.exclude_paths and "admin" in lib.cfg.ignore_dirs

    report = lib.scan()
    assert report.added == [BLP]


def test_opening_the_index_does_not_wait_for_a_writer(lib, tmp_path):
    """The page opens the index on every request. Once the index is current,
    opening it only reads, so it never waits while a scan holds the write lock."""
    import sqlite3
    import time
    cfg = config.load()
    index.connect(cfg).close()                     # create it at the current version
    writer = sqlite3.connect(cfg.index_db, isolation_level=None)
    writer.execute("BEGIN IMMEDIATE")              # a scan, mid-write
    try:
        started = time.monotonic()
        conn = index.connect(cfg)
        assert conn.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 0
        assert time.monotonic() - started < 1      # the old connect() waited up to 30 s
    finally:
        writer.execute("ROLLBACK")


def test_an_index_missing_a_column_is_still_upgraded(lib):
    """The read-only shortcut must not skip a needed change."""
    cfg = config.load()
    conn = index.connect(cfg)
    conn.execute("ALTER TABLE files DROP COLUMN queued")
    conn.commit()
    conn.close()
    have = {r["name"] for r in index.connect(cfg).execute("PRAGMA table_info(files)")}
    assert "queued" in have


def test_move_then_edit_between_scans_is_one_file(lib):
    # Filed into a course folder, then annotated, all while refs was not
    # looking: the inode survives, size and time do not.
    lib.write("papers-dump/blp.pdf", b"blp content")
    lib.scan()
    old_hash = lib.conn.execute("SELECT hash FROM paths").fetchone()["hash"]
    target = lib.root / BLP
    target.parent.mkdir(parents=True)
    os.rename(lib.root / "papers-dump/blp.pdf", target)
    with target.open("ab") as handle:                    # same inode, new bytes
        handle.write(b", now with highlights")

    report = lib.scan()
    assert report.moved == [("papers-dump/blp.pdf", BLP)]
    assert report.modified == [BLP]
    assert report.added == [] and report.removed == []
    row = lib.conn.execute(
        "SELECT f.hash, f.supersedes FROM paths p JOIN files f ON f.hash = p.hash").fetchone()
    assert row["hash"] != old_hash and row["supersedes"] == old_hash
    kinds = [r["kind"] for r in lib.conn.execute("SELECT kind FROM events ORDER BY id")]
    assert kinds[-2:] == ["moved", "modified"]


def test_new_file_with_known_content_is_not_taken_for_a_moved_edit(lib):
    # B4 applies only to new content: a vanished path whose inode turns up
    # holding bytes refs already knows is an ordinary move by content.
    lib.write("papers-dump/a.pdf", b"first")
    lib.write("papers-dump/b.pdf", b"second")
    lib.scan()
    (lib.root / "papers-dump/b.pdf").unlink()
    os.rename(lib.root / "papers-dump/a.pdf", lib.root / "papers-dump/c.pdf")
    (lib.root / "papers-dump/c.pdf").write_bytes(b"second")   # same inode, b's bytes

    report = lib.scan()
    assert report.modified == []
    assert lib.paths() == {"papers-dump/c.pdf"}
