"""Collections: the registry, memberships on works, folder-derived courses and
projects, the #name / course: / project: filters, library.bib, and the CLI."""

import json
import os
from types import SimpleNamespace

import pytest

from refs import cli, collection, config, contexts, index, librarybib, records, scan, works

BLP = "phd/coursework/2026-fall/220a/readings/03-demand/berry-1995-automobile-prices.pdf"
SALIENCE = "papers-dump/chetty-2009-salience.pdf"
BETS = "phd/research/bets/readings/lit/odonoghue-2001-choice.pdf"


@pytest.fixture
def lib(tmp_path, monkeypatch):
    """Three read papers: one in a course folder, one in the quick-add folder,
    one in a project folder. Scanned, so each has a work."""
    root = tmp_path / "Dropbox"
    monkeypatch.setenv("REFS_DROPBOX_ROOT", str(root))
    monkeypatch.setenv("REFS_INDEX_DB", str(tmp_path / "index.db"))
    (root / "papers-dump").mkdir(parents=True)
    papers = {BLP: ("Automobile Prices in Market Equilibrium", "Berry", 1995),
              SALIENCE: ("Salience and Taxation", "Chetty", 2009),
              BETS: ("Choice and Procrastination", "O'Donoghue", 2001)}
    for n, rel in enumerate(papers):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_bytes(f"pdf {n}".encode())
    cfg = config.load()
    conn = index.connect(cfg)
    scan.scan(cfg, conn)
    hashes = {r["path"]: r["hash"] for r in conn.execute("SELECT path, hash FROM paths")}
    for rel, (title, family, year) in papers.items():
        reading = {"kind": "paper", "title": title, "year": year, "venue": None,
                   "authors": [{"given": "A.", "family": family}], "is_working_paper": False,
                   "description": "", "fields": [], "methods": [], "topics": []}
        records.write_json(records.reads_dir(cfg) / f"{hashes[rel]}.json",
                           {"status": "extracted", "issues": [], "reading": reading, "registry": None})
    scan.scan(cfg, conn)
    work_of = {rel: conn.execute("SELECT work_id FROM files WHERE hash=?", (h,)).fetchone()[0]
               for rel, h in hashes.items()}
    return SimpleNamespace(cfg=cfg, conn=conn, root=root, work=work_of)


def titles(results):
    return sorted(r["title"] for r in results)


# --- Names and the registry ---------------------------------------------------------------

def test_names_are_cleaned_and_checked():
    assert collection.clean_name("#Econ 220A") == "econ-220a"
    assert collection.clean_name("219A / Public Economics") == "219a/public-economics"
    assert collection.clean_name("field_syllabi") == "field_syllabi"
    with pytest.raises(collection.Invalid):
        collection.clean_name("//")
    assert collection.matches("219a/public-economics", "219a")
    assert not collection.matches("219ab", "219a")


def test_register_keeps_what_is_not_given(lib):
    collection.register(lib.cfg, "bets", kind="project", folder="phd/research/bets/readings/lit",
                        shared=True, bib_file="~/phd/research/bets/refs.bib")
    collection.register(lib.cfg, "bets", term="")           # changes nothing else
    entry = collection.load(lib.cfg)["bets"]
    assert entry["shared"] and entry["folder"] == "phd/research/bets/readings/lit"
    assert entry["kind"] == "project" and entry["bib_file"] == "~/phd/research/bets/refs.bib"
    assert collection.register(lib.cfg, "220a", term="Fall 2026")["term"] == "2026-fall"
    assert collection.load(lib.cfg)["220a"]["kind"] == "course"         # a course code
    with pytest.raises(collection.Invalid):
        collection.register(lib.cfg, "x", folder="/Users/someone/x")
    # The registry carries the data version, like every file refs keeps in Dropbox.
    assert json.loads(collection.registry_path(lib.cfg).read_text())["schema"] == records.SCHEMA


def test_folder_contexts_stand_for_collections():
    layout = contexts.Layout([], ["phd", "papers-dump"], "papers-dump", "phd")
    assert collection.implicit(layout.derive(BLP)) == ("220a", "course", "2026-fall")
    assert collection.implicit(layout.derive(BETS)) == ("bets", "project", None)
    assert collection.implicit(layout.derive("phd/coursework/2025-summer/notes.pdf")) == \
        ("2025-summer", "course", "2025-summer")
    assert collection.implicit(layout.derive(SALIENCE)) is None          # the library itself
    assert collection.implicit(layout.derive("phd/stray.pdf")) is None


# --- Membership and search -----------------------------------------------------------------

def test_folder_collections_are_searchable_without_any_setup(lib):
    assert titles(index.search(lib.conn, [], {"collection": "220a"})) == \
        ["Automobile Prices in Market Equilibrium"]
    assert titles(index.search(lib.conn, [], {"project": "bets"})) == ["Choice and Procrastination"]
    found = index.search(lib.conn, [], {"course": "220a"})[0]
    assert found["collections"] == ["220a"]


def test_explicit_membership_follows_the_paper_not_the_folder(lib):
    salience = lib.work[SALIENCE]
    assert collection.change(lib.cfg, [salience], "219a/public-economics") == [salience]
    assert collection.change(lib.cfg, [salience], "219a/public-economics") == []   # once is enough
    assert collection.load(lib.cfg)["219a/public-economics"]["kind"] == "course"
    index.update_collections(lib.conn, works.load_all(lib.cfg)[salience])

    # A parent takes in its sub-collections; #name, course: and project: agree.
    for filters in ({"collection": "219a"}, {"course": "219a"}, {"collection": "219a/public-economics"}):
        assert titles(index.search(lib.conn, [], filters)) == ["Salience and Taxation"]
    assert index.search(lib.conn, [], {"collection": "219a/other"}) == []

    # Moving the PDF elsewhere keeps the membership; a rescan agrees.
    os.rename(lib.root / SALIENCE, lib.root / "phd/coursework/2026-fall/220a/salience.pdf")
    scan.scan(lib.cfg, lib.conn)
    found = index.search(lib.conn, ["salience"])[0]
    assert found["collections"] == ["219a/public-economics", "220a"]
    # Several #names must all hold.
    assert titles(index.search(lib.conn, [], {"collection": "219a,220a"})) == ["Salience and Taxation"]

    # Taking it out of the parent takes it out of the parts too; only the
    # explicit membership goes (220a stays: the PDF is in 220a's folder).
    assert collection.change(lib.cfg, [salience], "219a", add=False) == [salience]
    assert index.update_collections(lib.conn, works.load_all(lib.cfg)[salience]) == ["220a"]


def test_hash_names_in_a_query_become_a_collection_filter():
    assert cli._split_query(["demand", "#220A", "#bets", "year:1995"]) == \
        (["demand"], {"collection": "220a,bets", "year": "1995"})
    assert cli._split_query(["#"]) == (["#"], {})


def test_rename_moves_sub_collections_and_memberships(lib):
    salience = lib.work[SALIENCE]
    collection.change(lib.cfg, [salience], "219a/pf")
    assert collection.rename(lib.cfg, "219a", "219b") == 1
    assert works.load_all(lib.cfg)[salience]["user"]["collections"] == ["219b/pf"]
    assert set(collection.load(lib.cfg)) == {"219b/pf"}


def test_listing_counts_papers_and_marks_folder_born_collections(lib):
    collection.change(lib.cfg, [lib.work[SALIENCE]], "bets")
    collection.register(lib.cfg, "thesis")                       # registered, still empty
    scan.scan(lib.cfg, lib.conn)
    rows = {r["name"]: r for r in collection.listing(lib.cfg, lib.conn)}
    assert rows["220a"]["implicit"] and rows["220a"]["term"] == "2026-fall"
    assert rows["220a"]["kind"] == "course" and rows["220a"]["works"] == 1
    assert rows["bets"]["works"] == 2 and not rows["bets"]["implicit"]
    assert rows["thesis"]["works"] == 0 and rows["thesis"]["registered"]


# --- library.bib ------------------------------------------------------------------------------

def test_library_bib_has_every_paper_with_its_pdf_and_collections(lib):
    collection.change(lib.cfg, [lib.work[SALIENCE]], "219a")
    collection.change(lib.cfg, [lib.work[BETS]], "thesis")
    scan.scan(lib.cfg, lib.conn)
    assert librarybib.write(lib.cfg, lib.conn)
    assert not librarybib.write(lib.cfg, lib.conn)                # unchanged: not rewritten
    text = librarybib.path(lib.cfg).read_text()
    assert text.count("\n@") + text.startswith("@") == 3
    # Relative to the .bib (in the state folder), in JabRef's format.
    state = lib.cfg.absolute(lib.cfg.state_dir)
    rel = os.path.relpath(lib.root / SALIENCE, state)
    assert f"file     = {{:{rel}:PDF}}" in text
    assert (state / rel).resolve() == (lib.root / SALIENCE).resolve()
    assert "keywords = {219a}" in text
    assert "keywords = {thesis, bets}" in text                   # yours first, then the folder's
    assert "keywords = {220a}" in text


def test_jabref_escapes_colons_in_file_names():
    assert librarybib._escape_jabref("a:b;c\\d.pdf") == "a\\:b\\;c\\\\d.pdf"


def test_exporter_writes_after_a_poke(lib):
    exporter = librarybib.Exporter(lib.cfg, debounce=0.01, check=0.05)
    assert exporter.run_once(force=True)
    assert not exporter.run_once()                                # nothing changed
    collection.change(lib.cfg, [lib.work[SALIENCE]], "new-one")
    assert exporter.run_once()                                    # a work file changed
    assert "new-one" in librarybib.path(lib.cfg).read_text()


# --- The command line -------------------------------------------------------------------------

def test_project_new_registers_and_makes_the_folder(lib, capsys):
    assert cli.main(["project", "new", "Bets", "--make-folder", "--shared",
                     "--bib", "~/phd/research/bets/refs.bib"]) == 0
    entry = collection.load(lib.cfg)["bets"]
    assert entry["folder"] == "phd/research/bets/readings" and entry["shared"]
    assert (lib.root / "phd/research/bets/readings").is_dir()
    assert cli.main(["project", "new", "220a", "--course", "--term", "2026-fall"]) == 0
    assert collection.load(lib.cfg)["220a"] | {"created": None} == {
        "kind": "course", "term": "2026-fall", "folder": None, "shared": False,
        "bib_file": None, "created": None}
    # Shared without a folder makes no sense.
    assert cli.main(["project", "new", "x", "--shared"]) == 2


def test_collection_add_remove_and_list(lib, capsys):
    assert cli.main(["collection", "add", "thesis", "salience"]) == 0
    assert "added to thesis" in capsys.readouterr().out
    assert works.load_all(lib.cfg)[lib.work[SALIENCE]]["user"]["collections"] == ["thesis"]
    assert titles(index.search(lib.conn, [], {"collection": "thesis"})) == ["Salience and Taxation"]

    assert cli.main(["collection", "list", "--json"]) == 0
    names = {r["name"] for r in json.loads(capsys.readouterr().out)}
    assert {"220a", "bets", "thesis"} <= names

    assert cli.main(["collection", "remove", "220a", "automobile"]) == 0
    assert "stays in it until the file moves" in capsys.readouterr().out
    assert cli.main(["collection", "remove", "thesis", "salience"]) == 0
    assert works.load_all(lib.cfg)[lib.work[SALIENCE]]["user"]["collections"] == []


def test_bib_append_uses_the_collections_bib_file(lib, tmp_path, capsys):
    target = lib.root / "phd/research/bets/refs.bib"
    collection.register(lib.cfg, "bets", bib_file="phd/research/bets/refs.bib")
    assert cli.main(["bib", "#bets", "--append"]) == 0
    assert "@unpublished{odonoghue2001choice" in target.read_text()
    assert cli.main(["bib", "#220a", "--append"]) == 2          # 220a has no .bib


def test_delete_forgets_the_collection_and_its_parts(lib):
    salience = lib.work[SALIENCE]
    collection.change(lib.cfg, [salience], "219a/pf")
    collection.change(lib.cfg, [salience], "thesis")
    assert collection.delete(lib.cfg, "219a") == 1
    assert works.load_all(lib.cfg)[salience]["user"]["collections"] == ["thesis"]
    assert set(collection.load(lib.cfg)) == {"thesis"}
