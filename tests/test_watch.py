"""The folder watcher's pass: scan, read what is new, rename inside the dump.

The model and registries are replaced by a fake pipeline that writes a read
record the way the real one would, so these tests cost nothing and run offline.
"""

import os

import pytest

from refs import config, index, ingest, llm, pipeline, records, rename
from refs.watch import Watcher

COMPLETE = b"%PDF-1.4\n...pages...\n%%EOF\n"
TITLE = "Automobile Prices in Market Equilibrium"
CANONICAL = "berry-levinsohn-pakes-1995-automobile-prices-in-market-equilibrium.pdf"


@pytest.fixture
def lib(tmp_path, monkeypatch):
    root = tmp_path / "Dropbox"
    (root / "phd").mkdir(parents=True)
    (root / "papers-dump").mkdir()
    monkeypatch.setenv("REFS_DROPBOX_ROOT", str(root))
    monkeypatch.setenv("REFS_INDEX_DB", str(tmp_path / "index.db"))
    monkeypatch.setenv("REFS_CONFIG", str(tmp_path / "none.toml"))

    calls = []

    def fake_process(cfg, api, file_hash, rel, topics=None, previous=None):
        """Stands in for the model + registry: everything is BLP, verified."""
        calls.append(rel)
        authors = [{"given": "S.", "family": f} for f in ("Berry", "Levinsohn", "Pakes")]
        read = {
            "hash": file_hash, "path": rel, "status": "verified", "issues": [], "usage": {},
            "reused_reading": False,
            "reading": {"kind": "paper", "title": TITLE, "year": 1995, "venue": "Econometrica",
                        "authors": authors, "is_working_paper": False, "description": "",
                        "fields": [], "methods": [], "topics": []},
            "registry": {"via": "doi", "record": {
                "title": TITLE, "year": 1995, "venue": "Econometrica", "doi": "10.2307/2171802",
                "type": "journal-article", "authors": authors, "volume": "63", "issue": "4",
                "pages": "841-890", "publisher": "JSTOR", "source": "crossref"}},
        }
        records.write_json(records.reads_dir(cfg) / f"{file_hash}.json", read)
        return read

    monkeypatch.setattr(pipeline, "process_file", fake_process)
    monkeypatch.setattr(llm, "client", lambda: object())

    class Lib:
        def __init__(self):
            self.root, self.calls = root, calls

        def watcher(self, **settings):
            cfg = config.load()
            if settings:
                cfg = config.Config(**{**cfg.__dict__, **settings})
            return Watcher(cfg, notifier=None)

        def write(self, rel, content=COMPLETE):
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
            return path

    return Lib()


def test_new_pdf_in_dump_is_read_once_and_renamed(lib):
    lib.write("papers-dump/w4264.pdf")
    [item] = lib.watcher().run_pass()
    assert item.processed_now and item.rel == f"papers-dump/{CANONICAL}"
    assert (lib.root / "papers-dump" / CANONICAL).exists()
    assert not (lib.root / "papers-dump/w4264.pdf").exists()

    # A second pass finds nothing new: the model is not paid again.
    assert lib.watcher().run_pass() == []
    assert lib.calls == ["papers-dump/w4264.pdf"]


def test_outside_the_dump_files_are_read_but_never_renamed(lib):
    rel = "phd/coursework/2026-fall/220a/readings/blp.pdf"
    lib.write(rel)
    [item] = lib.watcher().run_pass()
    assert item.processed_now and item.rel == rel and (lib.root / rel).exists()


def test_auto_process_setting(lib):
    lib.write("phd/research/bets/readings/a.pdf", COMPLETE + b"a")
    lib.write("papers-dump/b.pdf", COMPLETE + b"b")
    assert [i.processed_now for i in lib.watcher(auto_process="none").run_pass()] == []
    done = lib.watcher(auto_process="dump").run_pass()
    assert [i.file_hash for i in done] and all(ingest.in_dump(config.load(), i.rel) for i in done)
    assert lib.calls == ["papers-dump/b.pdf"]


def test_incomplete_download_waits_for_the_rest(lib):
    path = lib.write("papers-dump/partial.pdf", b"%PDF-1.4\nhalf a file")
    [item] = lib.watcher().run_pass()
    assert item.skipped and item.read is None and lib.calls == []
    path.write_bytes(COMPLETE)                       # the download finishes
    [item] = lib.watcher().run_pass()
    assert item.processed_now and lib.calls == ["papers-dump/partial.pdf"]


def test_a_copy_of_a_known_file_is_not_read_again(lib):
    lib.write("papers-dump/w4264.pdf")
    lib.watcher().run_pass()
    lib.write("phd/coursework/2026-fall/220a/readings/copy.pdf")   # same bytes, new place
    assert lib.watcher().run_pass() == []
    assert len(lib.calls) == 1


def test_only_pdf_changes_outside_the_state_folder_matter(lib):
    watcher = lib.watcher()
    root = str(lib.root)
    assert watcher.relevant(f"{root}/papers-dump/x.pdf", False)
    assert watcher.relevant(f"{root}/papers-dump/X.PDF", False)
    assert watcher.relevant(f"{root}/phd/coursework/new-folder", True)
    assert not watcher.relevant(f"{root}/papers-dump/notes.txt", False)
    assert not watcher.relevant(f"{root}/papers-dump/._x.pdf", False)
    assert not watcher.relevant(f"{root}/phd/tools/refs/works/w1.json", False)
    assert not watcher.relevant(f"{root}/phd/tools/refs/reads/x.pdf", False)
    assert not watcher.relevant(f"{root}/phd/research/p/figures/plot.pdf", False)
    assert not watcher.relevant(f"{root}/phd/admission/visa.pdf", False)


def test_only_one_watcher_per_mac(lib):
    first, second = lib.watcher(), lib.watcher()
    try:
        assert first.start() is True
        assert second.start() is False
    finally:
        first.stop()


def test_free_name_keeps_an_existing_wp_suffix(tmp_path, monkeypatch):
    root = tmp_path / "Dropbox"
    (root / "papers-dump").mkdir(parents=True)
    monkeypatch.setenv("REFS_DROPBOX_ROOT", str(root))
    monkeypatch.setenv("REFS_INDEX_DB", str(tmp_path / "index.db"))
    monkeypatch.setenv("REFS_CONFIG", str(tmp_path / "none.toml"))
    cfg = config.load()
    published, wp = "verhoogen-2023-firm-level.pdf", "verhoogen-2023-firm-level-wp.pdf"
    for name in (published, wp, "w29461.pdf"):
        (root / "papers-dump" / name).write_bytes(name.encode())
    # The working paper already has its right name: leave it.
    assert rename.free_name(cfg, f"papers-dump/{wp}", published, True) is None
    # A second working-paper copy cannot take "-wp" (taken) and gets "-2".
    assert rename.free_name(cfg, "papers-dump/w29461.pdf", published, True) == \
        "verhoogen-2023-firm-level-2.pdf"
    # The published file itself is already right.
    assert rename.free_name(cfg, f"papers-dump/{published}", published, False) is None
    assert os.listdir(root / "papers-dump")          # nothing was renamed by asking


def test_the_page_sees_which_file_is_being_read_and_what_happened(lib, monkeypatch):
    """status() says the stage and the file while a PDF is read, then lists the
    outcome (with the rename inside the dump) and what the scan found."""
    lib.write("papers-dump/w4264.pdf")
    watcher = lib.watcher()
    seen = []
    real = pipeline.process_file

    def spying(cfg, api, file_hash, rel, topics=None, previous=None):
        seen.append(dict(watcher.status()["progress"]))      # what the page sees meanwhile
        return real(cfg, api, file_hash, rel, topics)
    monkeypatch.setattr(pipeline, "process_file", spying)
    watcher.run_pass()

    [during] = seen
    assert during["stage"] == "reading" and during["file"] == "papers-dump/w4264.pdf"
    assert (during["done"], during["total"]) == (0, 1) and during["since"] > 0

    status = watcher.status()
    assert status["progress"] is None and status["pass_started"] is None
    assert status["last_pass_seconds"] is not None
    read, scan = status["recent"]                             # newest first
    assert read["outcome"] == "read" and "Automobile Prices" in read["text"]
    assert read["path"] == f"papers-dump/{CANONICAL}"
    assert read["renamed_from"] == "papers-dump/w4264.pdf"
    assert scan["outcome"] == "scan" and "1 new" in scan["text"]


def test_a_skipped_file_is_listed_with_the_reason(lib):
    lib.write("papers-dump/partial.pdf", b"%PDF-1.4\nhalf a file")
    watcher = lib.watcher()
    watcher.run_pass()
    skipped = [e for e in watcher.status()["recent"] if e["outcome"] == "skipped"]
    assert len(skipped) == 1 and "incomplete" in skipped[0]["text"]
