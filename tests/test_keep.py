"""Which copy to keep (keep.py): the recommendation for each set of copies,
and doing what it says."""

import json

import pymupdf
import pytest
from fastapi.testclient import TestClient

from refs import config, index, keep, library, organize, scan, works
from refs.web.app import create_app

from test_organize import CANONICAL, write_read

H = {"X-Refs": "1"}
TITLE = "Automobile Prices in Market Equilibrium"


def pdf(text: str, pages: int = 10, marks: list[tuple[str, int]] = ()) -> bytes:
    """A real PDF of `pages` pages, with highlights by (author, page)."""
    doc = pymupdf.open()
    for n in range(pages):                    # adding a page frees the earlier page objects
        doc.new_page().insert_text((72, 72), f"{text} {n}")
    for n, (author, number) in enumerate(marks):
        page = doc[number]                    # kept while its mark is made
        annot = page.add_highlight_annot(pymupdf.Rect(72, 60 + 20 * n, 200, 76 + 20 * n))
        annot.set_info(title=author, modDate="D:20261101090000")
        annot.update()
    data = doc.tobytes()
    doc.close()
    return data


def read_record(cfg, conn, rel, *, wp=False, verified=False):
    """BLP, as a verified published record or an extracted working paper."""
    file_hash = conn.execute("SELECT hash FROM paths WHERE path=?", (rel,)).fetchone()[0]
    if verified:
        write_read(cfg, file_hash, rel)
        return
    read = {"status": "extracted", "issues": [], "registry": None,
            "reading": {"kind": "paper", "title": TITLE, "year": 1993, "venue": None, "is_working_paper": wp,
                        "authors": [{"given": "S.", "family": f} for f in ("Berry", "Levinsohn", "Pakes")],
                        "description": "", "fields": [], "methods": [], "topics": []}}
    path = cfg.absolute(cfg.state_dir) / "reads" / f"{file_hash}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(read))


@pytest.fixture
def lib(tmp_path, monkeypatch):
    root = tmp_path / "Dropbox"
    (root / "papers-dump").mkdir(parents=True)
    (tmp_path / "home/.Trash").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("REFS_DROPBOX_ROOT", str(root))
    monkeypatch.setenv("REFS_INDEX_DB", str(tmp_path / "index.db"))
    (tmp_path / "config.toml").write_text('roots = ["papers-dump"]\nannotation_authors = ["William"]\n')
    monkeypatch.setenv("REFS_CONFIG", str(tmp_path / "config.toml"))

    class Lib:
        def put(self, files: dict[str, bytes], records: dict[str, dict]):
            """Write files, scan, give them read records, scan again."""
            for rel, body in files.items():
                (root / rel).write_bytes(body)
            self.cfg = config.load()
            self.conn = index.connect(self.cfg)
            scan.scan(self.cfg, self.conn)
            for rel, options in records.items():
                read_record(self.cfg, self.conn, rel, **options)
            scan.scan(self.cfg, self.conn)
            [self.work] = [w for w in works.load_all(self.cfg).values() if "meta" in w]
            return self

        def advice(self):
            return keep.recommend(self.cfg, self.conn, works.load_all(self.cfg)[self.work["id"]],
                                  library.paths_of(self.conn, self.work["id"]))

        def actions(self):
            found = self.advice()
            return {c["path"].split("/")[-1]: ("★" if c["path"] == found["keep"] else c["action"])
                    for c in found["copies"]}
    lib = Lib()
    lib.root = root
    return lib


def test_the_copy_with_your_marks_is_kept_and_takes_the_tidy_name(lib):
    lib.put({f"papers-dump/{CANONICAL}": pdf("BLP"),
             "papers-dump/blp-marked.pdf": pdf("BLP", marks=[("William", 1), ("William", 2)])},
            {f"papers-dump/{CANONICAL}": {"verified": True}, "papers-dump/blp-marked.pdf": {"verified": True}})
    advice = lib.advice()
    assert lib.actions() == {"blp-marked.pdf": "★", CANONICAL: "trash"}
    assert advice["verdict"] == "safe" and advice["rename_to"] == CANONICAL
    assert "your marks" in advice["summary"]
    result = keep.apply(lib.cfg, lib.conn, works.load_all(lib.cfg)[lib.work["id"]],
                        library.paths_of(lib.conn, lib.work["id"]))
    assert result["trashed"] == [f"papers-dump/{CANONICAL}"]
    assert result["renamed"] == ("papers-dump/blp-marked.pdf", f"papers-dump/{CANONICAL}")
    # The kept (marked) file now has the tidy name; the clean one is in the Trash.
    doc = pymupdf.open(lib.root / "papers-dump" / CANONICAL)
    assert sum(len(list(p.annots())) for p in doc) == 2
    doc.close()
    assert works.load_all(lib.cfg)[lib.work["id"]]["user"]["former_names"] == ["blp-marked.pdf"]


def test_contained_marks_can_go_unique_marks_must_be_merged(lib):
    lib.put({"papers-dump/a.pdf": pdf("BLP", marks=[("William", 1)]),
             "papers-dump/b.pdf": pdf("BLP", marks=[("William", 1), ("William", 2), ("Jane", 3)]),
             "papers-dump/c.pdf": pdf("BLP", marks=[("Jane", 4), ("Jane", 5)])},
            {f"papers-dump/{n}.pdf": {"verified": True} for n in "abc"})
    assert lib.actions() == {"b.pdf": "★", "a.pdf": "trash", "c.pdf": "merge"}
    assert lib.advice()["verdict"] == "merge"
    # Doing it never trashes the copy with marks of its own.
    result = keep.apply(lib.cfg, lib.conn, works.load_all(lib.cfg)[lib.work["id"]],
                        library.paths_of(lib.conn, lib.work["id"]), optional=True)
    assert result["trashed"] == ["papers-dump/a.pdf"] and (lib.root / "papers-dump/c.pdf").exists()


def test_the_same_file_twice(lib):
    body = pdf("BLP")
    lib.put({f"papers-dump/{CANONICAL}": body, "papers-dump/download (1).pdf": body},
            {f"papers-dump/{CANONICAL}": {"verified": True}})
    assert lib.actions() == {CANONICAL: "★", "download (1).pdf": "trash"}
    assert lib.advice()["copies"][1]["why"] == "the same file"


def test_the_working_paper_is_only_a_suggestion(lib):
    lib.put({f"papers-dump/{CANONICAL}": pdf("BLP published", pages=10),
             "papers-dump/blp-wp.pdf": pdf("BLP draft", pages=40)},
            {f"papers-dump/{CANONICAL}": {"verified": True}, "papers-dump/blp-wp.pdf": {"wp": True}})
    advice = lib.advice()
    assert lib.actions() == {CANONICAL: "★", "blp-wp.pdf": "trash?"}
    assert advice["verdict"] == "optional" and "an appendix?" in advice["copies"][1]["why"]
    work = works.load_all(lib.cfg)[lib.work["id"]]
    paths = library.paths_of(lib.conn, lib.work["id"])
    # "Clear the safe sets" leaves it; doing this one set trashes it (another version stays).
    assert keep.apply(lib.cfg, lib.conn, work, paths)["trashed"] == []
    assert keep.apply(lib.cfg, lib.conn, work, paths, optional=True)["trashed"] == ["papers-dump/blp-wp.pdf"]


def test_your_marks_on_the_working_paper_keep_it(lib):
    lib.put({f"papers-dump/{CANONICAL}": pdf("BLP published", pages=10),
             "papers-dump/blp-wp.pdf": pdf("BLP draft", pages=40, marks=[("William", 3), ("William", 4)])},
            {f"papers-dump/{CANONICAL}": {"verified": True}, "papers-dump/blp-wp.pdf": {"wp": True}})
    # You read the working paper: it is the one kept, the published one a suggestion.
    assert lib.actions() == {"blp-wp.pdf": "★", CANONICAL: "trash?"}


def test_a_short_file_next_to_a_full_one_is_a_preview(lib):
    lib.put({f"papers-dump/{CANONICAL}": pdf("BLP first page", pages=3),
             "papers-dump/blp-full.pdf": pdf("BLP draft", pages=40)},
            {f"papers-dump/{CANONICAL}": {"verified": True}, "papers-dump/blp-full.pdf": {"wp": True}})
    advice = lib.advice()
    assert lib.actions() == {"blp-full.pdf": "★", CANONICAL: "trash?"}
    assert "a preview?" in advice["copies"][1]["why"]


def test_trash_never_takes_a_papers_last_file(lib):
    lib.put({f"papers-dump/{CANONICAL}": pdf("BLP")}, {f"papers-dump/{CANONICAL}": {"verified": True}})
    with pytest.raises(organize.Refused, match="only copy"):
        organize.trash(lib.cfg, lib.conn, f"papers-dump/{CANONICAL}", other_version_ok=True)


def test_the_page_shows_and_applies_recommendations(lib):
    lib.put({f"papers-dump/{CANONICAL}": pdf("BLP"),
             "papers-dump/blp-marked.pdf": pdf("BLP", marks=[("William", 1)])},
            {f"papers-dump/{CANONICAL}": {"verified": True}, "papers-dump/blp-marked.pdf": {"verified": True}})
    client = TestClient(create_app())
    [item] = client.get("/api/copies").json()["items"]
    assert item["advice"]["verdict"] == "safe" and item["advice"]["keep"] == "papers-dump/blp-marked.pdf"
    answer = client.post("/api/copies/apply", json={"work_ids": [item["work_id"]]}, headers=H).json()
    assert answer["trashed"] == [f"papers-dump/{CANONICAL}"] and len(answer["renamed"]) == 1
    assert client.get("/api/copies").json()["items"] == []
