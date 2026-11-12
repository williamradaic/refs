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
