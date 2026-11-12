"""Works: merging files of the same paper, stable citekeys, BibTeX."""

import json

import pytest

from refs import bib, config, works


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    root = tmp_path / "Dropbox"
    (root / "phd").mkdir(parents=True)
    monkeypatch.setenv("REFS_DROPBOX_ROOT", str(root))
    monkeypatch.setenv("REFS_INDEX_DB", str(tmp_path / "index.db"))
    monkeypatch.setenv("REFS_CONFIG", str(tmp_path / "none.toml"))
    return config.load()


def write_read(cfg, file_hash, *, title, family, year, status="verified", doi=None,
               venue=None, wp=False, kind="paper", record_type="journal-article"):
    """Save a minimal read record, as the pipeline would."""
    reading = {"kind": kind, "title": title, "year": year, "venue": venue,
               "authors": [{"given": "A.", "family": family}], "is_working_paper": wp,
               "description": "About something.", "fields": ["industrial organization"],
               "methods": [], "topics": ["demand estimation"]}
    read = {"status": status, "issues": [], "reading": reading, "registry": None}
    if status == "verified":
        read["registry"] = {"via": "doi", "record": {
            "title": title, "year": year, "venue": venue, "doi": doi, "type": record_type,
            "authors": [{"given": "Steven", "family": family}], "volume": "63", "issue": "4",
            "pages": "841-890", "publisher": "JSTOR", "source": "crossref"}}
    path = cfg.absolute(cfg.state_dir) / "reads" / f"{file_hash}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(read))


def test_working_paper_and_published_version_become_one_work(cfg):
    write_read(cfg, "aaa", title="Automobile Prices in Market Equilibrium", family="Berry",
               year=1995, doi="10.2307/2171802", venue="Econometrica")
    # A draft of the same paper that no registry confirmed: same title and first author.
    write_read(cfg, "bbb", title="Automobile prices in market equilibrium", family="Berry",
               year=1993, status="extracted", wp=True)
    write_read(cfg, "ccc", title="Something Else Entirely", family="Nevo", year=2001,
               doi="10.1111/x", venue="Econometrica")
    write_read(cfg, "ddd", title="Week 4 slides", family="Handel", year=2026,
               status="classified", kind="slides")

    mapping = works.sync(cfg)
    assert mapping["aaa"] == mapping["bbb"] != mapping["ccc"]
    assert "ddd" not in mapping                       # slides are not library works

    blp = works.load_all(cfg)[mapping["aaa"]]
    assert blp["citekey"] == "berry1995automobile"
    assert sorted(blp["files"]) == ["aaa", "bbb"]
    assert blp["meta"]["year"] == 1995 and blp["meta"]["venue"] == "Econometrica"
    assert blp["user"]["read_status"] == "unread"


def test_sync_is_stable_and_keeps_user_data(cfg):
    write_read(cfg, "aaa", title="Automobile Prices in Market Equilibrium", family="Berry",
               year=1995, doi="10.2307/2171802", venue="Econometrica")
    first = works.sync(cfg)
    work = works.load_all(cfg)[first["aaa"]]
    work["user"].update(read_status="read", notes="the BLP paper")
    (works.works_dir(cfg) / f"{work['id']}.json").write_text(json.dumps(work))

    # The registry record later changes (say, a corrected year): same work, same key.
    write_read(cfg, "aaa", title="Automobile Prices in Market Equilibrium", family="Berry",
               year=1996, doi="10.2307/2171802", venue="Econometrica")
    second = works.sync(cfg)
    after = works.load_all(cfg)[second["aaa"]]
    assert second == first and after["citekey"] == "berry1995automobile"
    assert after["meta"]["year"] == 1996
    assert after["user"]["notes"] == "the BLP paper" and after["user"]["read_status"] == "read"


def test_citekeys_do_not_collide(cfg):
    write_read(cfg, "aaa", title="Taxing the Rich", family="Saez", year=2017, doi="10.1/a")
    write_read(cfg, "bbb", title="Taxing Top Incomes", family="Saez", year=2017, doi="10.1/b")
    works.sync(cfg)
    assert sorted(w["citekey"] for w in works.load_all(cfg).values()) == [
        "saez2017taxing", "saez2017taxingb"]


def test_bibtex_entry(cfg):
    write_read(cfg, "aaa", title="Automobile Prices in Market Equilibrium", family="Berry",
               year=1995, doi="10.2307/2171802", venue="Econometrica")
    work_id = works.sync(cfg)["aaa"]
    text = bib.entry(works.load_all(cfg)[work_id])
    assert text.startswith("@article{berry1995automobile,")
    assert "author  = {Berry, Steven}" in text
    assert "journal = {Econometrica}" in text
    assert "pages   = {841--890}" in text
    assert "publisher" not in text                    # not wanted on articles


def test_capture_recognises_identifiers_and_links():
    from refs import capture
    assert capture.parse("https://www.nber.org/papers/w32515").ids == {"nber": "32515"}
    assert capture.parse("w32515").ids == {"nber": "32515"}
    assert capture.parse("https://arxiv.org/abs/2301.01234v2").ids == {"arxiv": "2301.01234"}
    assert capture.parse("2301.01234").ids == {"arxiv": "2301.01234"}
    assert capture.parse("https://doi.org/10.1257/aer.20220276").ids == {"doi": "10.1257/aer.20220276"}
    direct = capture.parse("https://example.edu/~someone/jmp.pdf")
    assert direct.pdf_url and not direct.ids
    page = capture.parse("https://example.org/article/123")
    assert page.page_url and not page.pdf_url


def test_stub_work_is_completed_when_its_pdf_arrives(cfg):
    record = {"title": "Automobile Prices in Market Equilibrium", "year": 1995,
              "venue": "Econometrica", "doi": "10.2307/2171802", "type": "journal-article",
              "authors": [{"given": "Steven", "family": "Berry"}], "source": "crossref"}
    stub = works.create_stub(cfg, record, "https://example.org/blp")
    assert stub["files"] == [] and stub["citekey"] == "berry1995automobile"
    assert works.create_stub(cfg, record, None)["id"] == stub["id"]      # no duplicate

    write_read(cfg, "aaa", title="Automobile Prices in Market Equilibrium", family="Berry",
               year=1995, doi="10.2307/2171802", venue="Econometrica")
    mapping = works.sync(cfg)
    assert mapping["aaa"] == stub["id"]
    done = works.load_all(cfg)[stub["id"]]
    assert done["files"] == ["aaa"] and done["user"]["source_url"] == "https://example.org/blp"


def test_user_edits_override_and_survive_resync(cfg):
    # A needs-review item whose registry "venue" is a repository.
    write_read(cfg, "aaa", title="Psychology and Economics", family="Rabin", year=1997,
               doi=None, venue="RePEc: Research Papers in Economics", record_type="article")
    work_id = works.sync(cfg)["aaa"]
    work = works.load_all(cfg)[work_id]
    assert work["meta"]["venue"] is None                 # a repository is not a venue
    assert work["meta"]["is_working_paper"]

    work["user"]["overrides"] = {"year": 1998, "venue": "Journal of Economic Literature",
                                 "type": "journal-article", "volume": "36", "pages": "11-46"}
    (works.works_dir(cfg) / f"{work_id}.json").write_text(json.dumps(work))
    works.sync(cfg)                                      # re-processing must not undo the edit
    edited = works.load_all(cfg)[work_id]
    assert edited["meta"]["status"] == "confirmed" and edited["meta"]["year"] == 1998
    assert edited["meta"]["source"] == "you" and not edited["meta"]["is_working_paper"]
    assert "journal = {Journal of Economic Literature}" in bib.entry(edited)
    assert edited["citekey"] == "rabin1997psychology"    # the key itself never changes


def test_venue_labels_and_abbreviations():
    from refs import venues
    assert venues.abbreviation("The Quarterly Journal of Economics") == "QJE"
    assert venues.abbreviation("Journal of Economic Behavior &amp; Organization") == "JEBO"
    assert venues.abbreviation("Some Obscure Journal") is None
    assert venues.clean("RePEc: Research Papers in Economics") is None
    assert venues.label({"kind": "book", "venue": None}) == "book"
    assert venues.label({"kind": "chapter", "venue": "Handbook of Econometrics"}) == "in Handbook of Econometrics"
    assert venues.label({"kind": "paper", "doi": "10.3386/w32515", "type": "report"}) == "NBER working paper"
    assert venues.label({"kind": "paper", "venue": "Econometrica", "type": "journal-article"}) == "Econometrica"
    assert venues.label({"kind": "paper", "venue": None, "is_working_paper": True}) == "working paper"


def test_importer_joins_existing_work_and_is_idempotent(cfg):
    write_read(cfg, "aaa", title="Automobile Prices in Market Equilibrium", family="Berry",
               year=1995, doi="10.2307/2171802", venue="Econometrica")
    existing = works.sync(cfg)["aaa"]
    blp = {"title": "Automobile prices in market equilibrium", "year": 1995, "doi": None,
           "authors": [{"given": "Steven", "family": "Berry"}], "venue": "Econometrica",
           "type": "journal-article", "kind": "paper", "source": "zotero"}
    new = {"title": "A Paper Only Zotero Knows", "year": 2020, "doi": "10.1/zzz",
           "authors": [{"given": "A.", "family": "Nobody"}], "venue": "SSRN Electronic Journal",
           "type": "posted-content", "kind": "paper", "source": "zotero"}
    for _ in range(2):                                   # importing twice changes nothing
        importer = works.Importer(cfg)
        for record, key in ((blp, "Z1"), (new, "Z2")):
            importer.add(record, file_hashes=[], external={"key": key, "citekey": f"zot{key}"},
                         tags=["bets"], collections=["Credit Cards!"], notes=["a note"])
        importer.save()
    all_works = works.load_all(cfg)
    assert len(all_works) == 2
    joined = all_works[existing]
    assert joined["meta"]["status"] == "verified"        # registry metadata is kept
    assert joined["user"]["tags"] == ["bets"] and joined["user"]["notes"] == "a note"
    assert joined["aliases"] == ["zotZ1"]
    created = next(w for w in all_works.values() if w["id"] != existing)
    assert created["meta"]["status"] == "imported" and created["meta"]["venue"] is None
    assert created["files"] == []


def test_keys_from_project_bib_files_become_search_keys(cfg, tmp_path, monkeypatch):
    from refs import index, scan
    write_read(cfg, "aaa", title="Automobile Prices in Market Equilibrium", family="Berry",
               year=1995, doi="10.2307/2171802", venue="Econometrica")
    write_read(cfg, "ccc", title="Measuring Market Power", family="Nevo", year=2001,
               doi=None, venue="Econometrica")
    project = cfg.dropbox_root / "phd/research/bets/paper"
    project.mkdir(parents=True)
    (project / "references.bib").write_text(
        "@article{BLP95, title={Automobile prices}, doi={10.2307/2171802}}\n"   # by DOI
        "@article{Nevo:01, title = {{Measuring} market power}}\n"               # by title
        "@article{Unknown2020, title={Something else}}\n")
    settings = tmp_path / "config.toml"
    settings.write_text('bib_files = ["phd/research/**/*.bib"]\n')
    monkeypatch.setenv("REFS_CONFIG", str(settings))
    cfg = config.load()

    by_file = works.sync(cfg)
    all_works = works.load_all(cfg)
    assert all_works[by_file["aaa"]]["bib_keys"] == {"BLP95": "Dropbox/phd/research/bets/paper/references.bib"}
    assert list(all_works[by_file["ccc"]]["bib_keys"]) == ["Nevo:01"]

    # The keys are searchable (these works have no PDF on disk, so they are
    # indexed as works without files).
    conn = index.connect(cfg)
    scan.scan(cfg, conn)
    assert [r["work_id"] for r in index.search(conn, ["BLP95"], {})] == [by_file["aaa"]]
    assert [r["work_id"] for r in index.search(conn, ["Nevo:01"], {})] == [by_file["ccc"]]

    # A key removed from the file is forgotten on the next sync.
    (project / "references.bib").write_text("")
    works.sync(cfg)
    assert "bib_keys" not in works.load_all(cfg)[by_file["aaa"]]
