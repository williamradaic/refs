"""Phase 3: moving every reading into the library folder, with undo and the switch."""

import tomllib

import pytest

from refs import collection, config, index, migrate, scan, works

from test_organize import CANONICAL, write_read
from test_shared import pdf_bytes

TOPIC = "phd/coursework/2026-fall/220a/readings/03-demand-estimation-ii"
SHARED = "phd/research/bets/readings/lit"


def read_paper(cfg, conn, rel, title, family, year):
    """An extracted read record for one file (no registry: keeps its file name)."""
    import json
    file_hash = conn.execute("SELECT hash FROM paths WHERE path=?", (rel,)).fetchone()[0]
    read = {"status": "extracted", "issues": [], "registry": None,
            "reading": {"kind": "paper", "title": title, "year": year, "venue": None,
                        "authors": [{"given": "A.", "family": family}], "is_working_paper": False,
                        "description": "", "fields": [], "methods": [], "topics": []}}
    path = cfg.absolute(cfg.state_dir) / "reads" / f"{file_hash}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(read))


@pytest.fixture
def lib(tmp_path, monkeypatch):
    """BLP (verified) only in a course topic folder, Laibson only in the shared
    bets folder, Chetty in the library and in a course folder."""
    root = tmp_path / "Dropbox"
    for folder in ("papers-dump", TOPIC, SHARED, "phd/coursework/2025-fall/219a/readings/01-salience"):
        (root / folder).mkdir(parents=True)
    (tmp_path / "home/.Trash").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("REFS_DROPBOX_ROOT", str(root))
    monkeypatch.setenv("REFS_INDEX_DB", str(tmp_path / "index.db"))
    config_file = tmp_path / "config.toml"
    config_file.write_text('# my settings\nroots = ["phd", "papers-dump"]\nlayout = "phd"\n'
                           'state_dir = "phd/tools/refs"\n')
    monkeypatch.setenv("REFS_CONFIG", str(config_file))
    salience = pdf_bytes("Salience")                 # one file in two places
    files = {f"{TOPIC}/blp.pdf": pdf_bytes("BLP"), f"{SHARED}/laibson.pdf": pdf_bytes("Golden eggs"),
             "papers-dump/chetty-2009-salience.pdf": salience,
             "phd/coursework/2025-fall/219a/readings/01-salience/chetty.pdf": salience}
    for rel, body in files.items():
        (root / rel).write_bytes(body)
    cfg = config.load()
    conn = index.connect(cfg)
    scan.scan(cfg, conn)
    write_read(cfg, conn.execute("SELECT hash FROM paths WHERE path=?", (f"{TOPIC}/blp.pdf",)).fetchone()[0],
               f"{TOPIC}/blp.pdf")
    read_paper(cfg, conn, f"{SHARED}/laibson.pdf", "Golden Eggs and Hyperbolic Discounting", "Laibson", 1997)
    read_paper(cfg, conn, "papers-dump/chetty-2009-salience.pdf", "Salience and Taxation", "Chetty", 2009)
    collection.register(cfg, "bets", kind="project", folder=SHARED, shared=True)
    cfg = config.load()
    scan.scan(cfg, conn)

    class Lib:
        pass
    lib = Lib()
    lib.root, lib.cfg, lib.conn, lib.config_file = root, cfg, conn, config_file
    lib.ids = {works.load_all(cfg)[w]["citekey"]: w for w in works.load_all(cfg)}
    return lib


def rows_by_key(the_plan):
    return {row.citekey: row for row in the_plan.rows}


def test_plan_moves_course_papers_copies_shared_ones_and_files_the_rest(lib):
    the_plan = migrate.plan(lib.cfg, lib.conn)
    rows = rows_by_key(the_plan)
    blp, laibson, chetty = rows["berry1995automobile"], rows["laibson1997golden"], rows["chetty2009salience"]
    assert (blp.mode, blp.source) == ("move", f"{TOPIC}/blp.pdf")
    assert blp.collections == ["220a/demand-estimation-ii"]
    assert the_plan.parts["220a/demand-estimation-ii"] == {"order": 3, "label": "Demand estimation II"}
    assert (laibson.mode, laibson.collections) == ("copy", ["bets"])
    assert chetty.source is None and chetty.collections == ["219a/salience"]
    assert the_plan.inboxes == ["phd/coursework"]


def test_apply_then_undo_puts_everything_back(lib):
    the_plan = migrate.plan(lib.cfg, lib.conn)
    result = migrate.apply(lib.cfg, lib.conn, the_plan, readings_md=True)
    assert len(result["done"]) == 3 and result["refused"] == []
    assert (lib.root / "papers-dump" / CANONICAL).is_file()           # verified: canonical name
    assert not (lib.root / TOPIC / "blp.pdf").exists()
    assert (lib.root / "papers-dump/laibson.pdf").is_file()           # extracted: its own name
    assert (lib.root / SHARED / "laibson.pdf").is_file()              # coauthors keep theirs
    blp = works.load_all(lib.cfg)[lib.ids["berry1995automobile"]]
    assert blp["user"]["collections"] == ["220a/demand-estimation-ii"]
    assert collection.load(lib.cfg)["220a/demand-estimation-ii"]["label"] == "Demand estimation II"
    readings = (lib.root / "phd/coursework/2026-fall/220a/readings.md").read_text()
    assert "## 03 Demand estimation II" in readings and "`berry1995automobile`" in readings
    scan.scan(lib.cfg, lib.conn)
    assert migrate.plan(lib.cfg, lib.conn).rows == []                 # nothing left to do

    assert migrate.undo(lib.cfg) == {"files": 2, "papers": 3}
    assert (lib.root / TOPIC / "blp.pdf").is_file() and not (lib.root / "papers-dump" / CANONICAL).exists()
    assert not (lib.root / "papers-dump/laibson.pdf").exists()
    assert works.load_all(lib.cfg)[lib.ids["berry1995automobile"]]["user"]["collections"] == []
    assert migrate.undo(lib.cfg) == {"files": 0, "papers": 0}       # nothing more to undo


def test_finish_renames_the_library_and_watches_it_alone(lib):
    migrate.apply(lib.cfg, lib.conn, migrate.plan(lib.cfg, lib.conn))
    scan.scan(lib.cfg, lib.conn)
    result = migrate.finish(lib.cfg, migrate.plan(lib.cfg, lib.conn), "refs-library")
    assert result["renamed"] == ("papers-dump", "refs-library")
    saved = tomllib.loads(lib.config_file.read_text())
    assert saved["dump"] == "refs-library" and saved["roots"] == ["refs-library"]
    assert saved["inboxes"] == ["phd/coursework"]
    assert lib.config_file.read_text().startswith("# my settings")    # comments kept
    cfg = config.load()
    assert cfg.watched == ["refs-library", SHARED]
    conn = index.connect(cfg)
    report = scan.scan(cfg, conn)
    assert len(report.moved) == 3                                      # recognised, not re-read
    found = index.search(conn, [], {"collection": "220a"})
    assert [f["citekey"] for f in found] == ["berry1995automobile"]
    assert found[0]["locations"][0]["path"] == f"refs-library/{CANONICAL}"
