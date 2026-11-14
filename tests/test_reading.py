"""Reading: read status detected from your markup, and the reading queue."""

import json

import pymupdf
import pytest

from refs import config, index, reading, scan, works

PAPER = "phd/coursework/2026-fall/220a/readings/03-demand/berry-1994-estimating-discrete-choice.pdf"


def make_pdf(path, pages: int, marks: dict[int, str] | None = None) -> None:
    """A PDF of `pages` pages; `marks` maps a page number to the author of one highlight on it."""
    doc = pymupdf.open()
    for n in range(pages):
        page = doc.new_page()
        page.insert_text((72, 72), f"Page {n + 1} of a paper about demand estimation.")
        if marks and n in marks:
            annot = page.add_highlight_annot(pymupdf.Rect(70, 60, 300, 80))
            annot.set_info(title=marks[n])
            annot.update()
    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(path)
    doc.close()


# --- the rule ---------------------------------------------------------------------------

@pytest.mark.parametrize("pages, marked, expected", [
    (40, 0, None), (40, 1, None),          # one page of marks: no sign either way
    (40, 2, "reading"), (40, 11, "reading"),
    (40, 12, "read"),                      # 30% of the pages
    (5, 3, "read"),                        # short paper: half its pages is enough
    (12, 4, "reading"),                    # 33%, but fewer than 5 pages
    (400, 300, "reading"),                 # a book is never "read" from marks
])
def test_detect(pages, marked, expected):
    assert reading.detect(pages, marked) == expected


def test_counts_only_your_marks(tmp_path):
    path = tmp_path / "a.pdf"
    make_pdf(path, 10, {0: "Me", 1: "me", 2: "", 3: "Instructor", 4: "Instructor"})
    pages, marked, marks = reading.count_marks(path, {"me", ""})
    assert (pages, marked, marks) == (10, 3, 3)      # case-insensitive; "" counts; others do not


def test_unreadable_file_counts_as_nothing(tmp_path):
    path = tmp_path / "broken.pdf"
    path.write_bytes(b"%PDF-1.4 not really")
    assert reading.count_marks(path, {"me"}) == (0, 0, 0)


# --- whose status wins ------------------------------------------------------------------

def work(**user):
    return {"user": {"read_status": "unread", **user}}


def test_detection_raises_but_your_choice_wins():
    w = work()
    reading.apply_detection(w, (40, 12, 30))
    assert reading.effective(w) == ("read", "detected")
    reading.set_status(w, "unread")                  # you disagree: that sticks
    assert reading.effective(w) == ("unread", "you")
    reading.apply_detection(w, (40, 30, 90))
    assert reading.effective(w) == ("unread", "you")


def test_detection_never_goes_down():
    w = work()
    reading.apply_detection(w, (40, 12, 30))
    reading.apply_detection(w, (40, 3, 3))           # say, only a lightly marked copy is left
    assert w["read_auto"]["status"] == "read" and w["read_auto"]["pages"] == 12


def test_reading_a_queued_paper_takes_it_off_the_queue():
    w = work()
    reading.set_queued(w, True)
    assert w["user"]["queued"]
    assert reading.apply_detection(w, (40, 5, 9)) is False      # started: stays queued
    assert w["user"]["queued"]
    assert reading.apply_detection(w, (40, 15, 40)) is True     # finished: leaves
    assert "queued" not in w["user"]

    # Queued again to re-read: an old detection does not drop it straight away.
    reading.set_queued(w, True)
    assert reading.apply_detection(w, (40, 15, 40)) is False
    assert w["user"]["queued"]
    reading.set_status(w, "read")                    # marking it read does
    assert "queued" not in w["user"]


# --- through a scan ---------------------------------------------------------------------

@pytest.fixture
def lib(tmp_path, monkeypatch):
    root = tmp_path / "Dropbox"
    (root / "phd").mkdir(parents=True)
    (root / "papers-dump").mkdir()
    monkeypatch.setenv("REFS_DROPBOX_ROOT", str(root))
    monkeypatch.setenv("REFS_INDEX_DB", str(tmp_path / "index.db"))
    monkeypatch.setenv("REFS_CONFIG", str(tmp_path / "none.toml"))
    monkeypatch.setitem(config.DEFAULTS, "annotation_authors", ["Me"])
    cfg = config.load()
    return cfg, index.connect(cfg)


def file_a_read_record(cfg, conn, rel):
    """Scan once, then save a read record for the file, as the pipeline would."""
    scan.scan(cfg, conn)
    file_hash = conn.execute("SELECT hash FROM paths WHERE path=?", (rel,)).fetchone()["hash"]
    reading_ = {"kind": "paper", "title": "Estimating Discrete-Choice Models of Product Differentiation",
                "year": 1994, "venue": "RAND", "authors": [{"given": "S.", "family": "Berry"}],
                "is_working_paper": False, "description": "", "fields": [], "methods": [], "topics": []}
    path = cfg.absolute(cfg.state_dir) / "reads" / f"{file_hash}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"status": "extracted", "issues": [], "reading": reading_, "registry": None}))
    scan.scan(cfg, conn)
    [w] = works.load_all(cfg).values()
    return w


def test_annotating_a_queued_paper_marks_it_read_and_dequeues_it(lib):
    cfg, conn = lib
    pdf = cfg.absolute(PAPER)
    make_pdf(pdf, 20)
    w = file_a_read_record(cfg, conn, PAPER)
    reading.set_queued(w, True)
    (works.works_dir(cfg) / f"{w['id']}.json").write_text(json.dumps(w))
    scan.scan(cfg, conn)
    assert [r["citekey"] for r in index.search(conn, [], {"queued": "yes"})] == [w["citekey"]]

    # First sitting: a few pages. Started, still queued.
    make_pdf(pdf, 20, {0: "Me", 1: "Me", 2: "Me"})
    report = scan.scan(cfg, conn)
    assert report.marks_counted == 1 and not report.finished
    assert index.search(conn, [], {"read": "reading"})[0]["queued"]

    # Second sitting: the chain of edited versions still leads to the work.
    make_pdf(pdf, 20, {n: "Me" for n in range(8)})
    report = scan.scan(cfg, conn)
    assert len(report.finished) == 1 and w["citekey"] in report.finished[0]
    after = works.load_all(cfg)[w["id"]]
    assert reading.effective(after) == ("read", "detected")
    assert after["read_auto"]["pages"] == 8 and "queued" not in after["user"]
    assert index.search(conn, [], {"read": "read"})[0]["work_id"] == w["id"]
    assert not index.search(conn, [], {"queued": "yes"})


def test_markup_is_counted_once_per_content(lib):
    cfg, conn = lib
    make_pdf(cfg.absolute(PAPER), 4, {0: "Me"})
    assert scan.scan(cfg, conn).marks_counted == 1
    assert scan.scan(cfg, conn).marks_counted == 0
