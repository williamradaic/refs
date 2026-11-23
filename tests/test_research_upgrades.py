"""Tests for the research-first upgrades: reading log, queue intent, tags, merge/remove,
PDF highlights, Obsidian sync/backlinks, WP->published updates, and resilient batch import."""

import dataclasses
import json
from pathlib import Path

import pymupdf
import pytest
from fastapi.testclient import TestClient

from refs import batch, bib, collection, config, highlights, index, notes, reading, resolve, scan, updates, works
from refs.web.app import create_app


@pytest.fixture
def env(tmp_path, monkeypatch):
    root = tmp_path / "Dropbox"
    (root / "refs-library").mkdir(parents=True)
    vault = tmp_path / "vault"
    (vault / ".obsidian").mkdir(parents=True)
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text(
        f'roots = ["refs-library"]\n'
        f'dump = "refs-library"\n'
        f'obsidian_vault = "{vault}"\n'
        f'obsidian_folder = "refs"\n'
        f'downloads_folder = ""\n'
        f'annotation_authors = ["Me", ""]\n'
    )
    monkeypatch.setenv("REFS_DROPBOX_ROOT", str(root))
    monkeypatch.setenv("REFS_INDEX_DB", str(tmp_path / "index.db"))
    monkeypatch.setenv("REFS_CONFIG", str(cfg_file))
    cfg = config.load()
    conn = index.connect(cfg)
    return cfg, conn, root, vault


def _write_read(cfg, file_hash, *, title, family, year, status="verified", doi=None,
                venue=None, wp=False, record_type="journal-article", topics=("demand estimation",)):
    rec = {
        "status": status, "issues": [],
        "reading": {
            "kind": "paper", "title": title, "year": year, "venue": venue,
            "authors": [{"given": "A.", "family": family}], "is_working_paper": wp,
            "description": "A paper summary.", "fields": ["industrial organization"],
            "methods": ["structural estimation"], "topics": list(topics),
        },
        "registry": None,
    }
    if status == "verified":
        rec["registry"] = {
            "via": "doi",
            "record": {
                "title": title, "year": year, "venue": venue, "doi": doi, "type": record_type,
                "authors": [{"given": "Steven", "family": family}],
                "volume": "63", "issue": "4", "pages": "841-890", "publisher": "Wiley",
                "source": "crossref",
            },
        }
    p = cfg.absolute(cfg.state_dir) / "reads" / f"{file_hash}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(rec))
    return rec


def _make_annotated_pdf(path: Path, text: str, comment: str = "", author: str = "Me") -> None:
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), text)
    hits = page.search_for(text.split()[0])
    rect = hits[0] if hits else pymupdf.Rect(70, 60, 320, 85)
    # Highlight the full line rect
    annot = page.add_highlight_annot(pymupdf.Rect(70, 58, 420, 82))
    annot.set_info(title=author, content=comment)
    annot.set_colors(stroke=(1.0, 0.92, 0.23))
    annot.update()
    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(path)
    doc.close()


# --- Phase 1: 4-state reading, personal/hidden tags, override keys, merge & remove ---

def test_skimmed_status_and_reading_log(env):
    cfg, conn, _, _ = env
    _write_read(cfg, "h1", title="Optimal Sin Taxes", family="Rabin", year=2006,
                doi="10.1/sin", venue="Journal of Public Economics")
    wid = works.sync(cfg)["h1"]
    w = works.load_one(cfg, wid)

    reading.set_status(w, "skimmed", context="220a")
    assert reading.effective(w) == ("skimmed", "you")
    assert len(w["user"]["reads"]) == 1
    assert w["user"]["reads"][0]["status"] == "skimmed"
    assert w["user"]["reads"][0]["context"] == "220a"
    assert reading.last_read_at(w) is not None

    # Re-read later for a project
    reading.log_read(w, "read", context="bets", note="checked tax incidence theorem", at="2026-11-23")
    assert len(w["user"]["reads"]) == 2
    assert w["user"]["reads"][-1]["note"] == "checked tax incidence theorem"


def test_override_keys_attach_incoming_pdf(env):
    cfg, _, _, _ = env
    _write_read(cfg, "h1", title="Early Working Draft", family="Smith", year=2024,
                status="extracted", wp=True)
    wid = works.sync(cfg)["h1"]
    w = works.load_one(cfg, wid)
    w["user"]["overrides"] = {"doi": "10.1257/aer.20250001", "title": "Final Published Title"}
    works.sync_keys(w)
    (works.works_dir(cfg) / f"{wid}.json").write_text(json.dumps(w))

    # A new PDF arrives with the corrected DOI and final title: attaches to the same work.
    _write_read(cfg, "h2", title="Final Published Title", family="Smith", year=2025,
                doi="10.1257/aer.20250001", venue="AER")
    mapping = works.sync(cfg)
    assert mapping["h2"] == wid


def test_personal_and_hidden_tags_survive_sync(env):
    cfg, conn, _, _ = env
    _write_read(cfg, "h1", title="Tax Policy", family="Saez", year=2019,
                doi="10.1/tax", venue="AER", topics=("taxation", "wrong-topic"))
    wid = works.sync(cfg)["h1"]
    works.edit_tags(cfg, wid, add_user=["seminal", "jmp-ref"], hide_auto=["wrong-topic"])
    works.sync(cfg)
    w = works.load_one(cfg, wid)
    assert "seminal" in w["user"]["tags"]
    assert "wrong-topic" not in w["tags"]
    assert "taxation" in w["tags"]

    # Restore hidden auto tags
    works.edit_tags(cfg, wid, restore_auto=True)
    w2 = works.load_one(cfg, wid)
    assert "wrong-topic" in w2["tags"]


def test_merge_and_remove_works(env):
    cfg, conn, _, _ = env
    _write_read(cfg, "h1", title="Published Paper", family="Berry", year=1995,
                doi="10.1/pub", venue="Econometrica")
    _write_read(cfg, "h2", title="Earlier Different WP Title", family="Berry", year=1993,
                status="extracted", wp=True)
    mapping = works.sync(cfg)
    w1, w2 = mapping["h1"], mapping["h2"]
    collection.change(cfg, [w2], "bets")
    works.edit_tags(cfg, w2, add_user=["classic"])
    w2_rec = works.load_one(cfg, w2)
    notes.write(cfg, w2_rec, "Old draft note.", "")

    res = works.merge(cfg, w1, w2)
    assert res["dropped"] == w2
    assert res["note_status"] == "copied"
    merged = works.load_one(cfg, w1)
    assert set(merged["files"]) == {"h1", "h2"}
    assert "bets" in collection.explicit(merged)
    assert "classic" in merged["user"]["tags"]
    assert res["dropped_citekey"] in merged["aliases"]
    assert works.load_one(cfg, w2) is None
    assert "Old draft note." in notes.read(cfg, merged)["body"]

    # Remove a stub work cleanly
    stub = works.create_stub(cfg, {"title": "Mistake Paper", "year": 2020,
                                   "authors": [{"given": "X", "family": "Y"}]}, None)
    works.remove(cfg, conn, stub["id"])
    assert works.load_one(cfg, stub["id"]) is None
    assert (cfg.absolute(cfg.state_dir) / "trash" / f"{stub['id']}.json").exists()


# --- Phase 2: Queue with intent & BibTeX acronym protection + append ---

def test_queue_intent_and_bib_acronym_bracing(env):
    cfg, conn, root, _ = env
    _write_read(cfg, "h1", title="Effects of US COVID-19 Policy on GDP and R&D in PhD Programs",
                family="Chetty", year=2022, doi="10.1/us", venue="QJE")
    wid = works.sync(cfg)["h1"]
    w = works.load_one(cfg, wid)

    entry = bib.entry(w)
    assert "{US}" in entry
    assert "{COVID-19}" in entry
    assert "{GDP}" in entry
    assert "{R\\&D}" in entry
    assert "{PhD}" in entry
    assert "{Effects}" not in entry

    # Already-braced and ALL CAPS titles are handled safely
    assert bib._protect_title("A Model of {AI} and OLS") == "A Model of {AI} and {OLS}"
    assert bib._protect_title("ALL CAPS TITLE") == "ALL CAPS TITLE"

    # Queue intent
    reading.set_queued(w, True, note="check IV spec", for_collection="bets", priority="next")
    assert w["user"]["queue_note"] == "check IV spec"
    assert w["user"]["queue_for"] == "bets"
    assert w["user"]["queue_priority"] == "next"
    reading.set_status(w, "read")
    assert "queued" not in w["user"] and "queue_note" not in w["user"]


def test_append_bib_endpoint(env):
    cfg, conn, root, _ = env
    _write_read(cfg, "h1", title="Demand for Ice Cream", family="Nevo", year=2001,
                doi="10.1/ice", venue="Econometrica")
    wid = works.sync(cfg)["h1"]
    scan.scan(cfg, conn)
    bib_file = root / "refs.bib"
    collection.register(cfg, "bets", kind="project", bib_file="refs.bib")
    collection.change(cfg, [wid], "bets")

    client = TestClient(create_app(watch=False))
    r = client.post("/api/collections/append-bib",
                    json={"name": "bets", "work_ids": [wid]}, headers={"X-Refs": "1"})
    assert r.status_code == 200
    assert r.json()["added"] == ["nevo2001demand"]
    assert "@article{nevo2001demand," in bib_file.read_text()

    # Running again skips duplicates
    r2 = client.post("/api/collections/append-bib",
                     json={"name": "bets", "work_ids": [wid]}, headers={"X-Refs": "1"})
    assert r2.json()["added"] == []
    assert len(r2.json()["present"]) == 1


# --- Phase 3: PDF Highlights + Obsidian sync & backlinks ---

def test_highlights_extracted_searched_and_pulled_idempotently(env):
    cfg, conn, root, vault = env
    pdf = root / "refs-library" / "berry-1995-blp.pdf"
    _make_annotated_pdf(pdf, "Unobserved product quality drives endogeneity in prices.",
                        comment="Crucial identification assumption for bets")
    scan.scan(cfg, conn)
    file_hash = conn.execute("SELECT hash FROM paths").fetchone()["hash"]
    _write_read(cfg, file_hash, title="Automobile Prices", family="Berry", year=1995,
                doi="10.1/blp", venue="Econometrica")
    scan.scan(cfg, conn)
    [wid] = list(works.load_all(cfg))
    w = works.load_one(cfg, wid)

    items = highlights.for_work(cfg, conn, wid)
    assert len(items) == 1
    assert "endogeneity" in items[0]["quote"].lower()
    assert "identification assumption" in items[0]["comment"]

    # Searchable via FTS5!
    hits = index.search(conn, ["endogeneity"], {})
    assert any(h["work_id"] == wid for h in hits)
    hits_comment = index.search(conn, ["identification", "assumption"], {})
    assert any(h["work_id"] == wid for h in hits_comment)

    # Pull into Obsidian note, add user prose above, pull again -> idempotent!
    n1 = notes.write(cfg, w, "My own synthesis paragraph.", "")
    n2 = notes.pull_highlights(cfg, w, items, n1["version"], collections=["bets"])
    assert "My own synthesis paragraph." in n2["body"]
    assert notes.HIGHLIGHTS_START in n2["body"]
    assert "Crucial identification assumption" in n2["body"]

    # Re-pull with updated highlights preserves user synthesis
    updated_items = [*items, {"page": 9, "kind": "Text", "color": None, "quote": "",
                              "comment": "Second margin note", "author": "Me", "date": None}]
    n3 = notes.pull_highlights(cfg, w, updated_items, n2["version"], collections=["bets"])
    assert n3["body"].count(notes.HIGHLIGHTS_START) == 1
    assert "My own synthesis paragraph." in n3["body"]
    assert "Second margin note" in n3["body"]


def test_obsidian_front_matter_sync_and_backlinks(env):
    cfg, conn, _, vault = env
    _write_read(cfg, "h1", title="Original WP Title", family="Rabin", year=2006,
                doi="10.1/rabin", venue="QJE")
    wid = works.sync(cfg)["h1"]
    w = works.load_one(cfg, wid)
    notes.write(cfg, w, "Some thoughts.", "")
    note_path = notes.path_for(cfg, w)
    # User adds custom Obsidian property
    note_path.write_text(note_path.read_text().replace("refs: " + wid + "\n",
                                                       f"refs: {wid}\nrating: 5\n"))
    collection.change(cfg, [wid], "bets")
    reading.set_status(w, "read")
    w["meta"]["venue"] = "Quarterly Journal of Economics"
    notes.sync_front_matter(cfg, w, collections=["bets"])
    synced = note_path.read_text()
    assert "rating: 5\n" in synced
    assert "read_status: read\n" in synced
    assert '- "bets"' in synced
    assert "Some thoughts." in synced

    # Backlinks from other vault notes
    (vault / "projects").mkdir()
    (vault / "projects" / "bets-outline.md").write_text(
        f"We build on [[{w['citekey']}|Rabin (2006)]] for our framework.\n"
    )
    links = notes.backlinks(cfg, w["citekey"])
    assert len(links) == 1
    assert links[0]["title"] == "bets-outline"
    assert links[0]["file"] == "projects/bets-outline.md"


# --- Phase 4: WP -> Published checker & Resilient Batch Import ---

def test_wp_published_updates_workflow(env, monkeypatch):
    cfg, conn, _, _ = env
    _write_read(cfg, "h1", title="Behavioral Hazard in Health Insurance", family="Baicker",
                year=2013, status="extracted", wp=True, venue="NBER Working Paper")
    wid = works.sync(cfg)["h1"]

    fake_pub = resolve.Record(
        title="Behavioral Hazard in Health Insurance",
        authors=[{"given": "Katherine", "family": "Baicker"}],
        year=2015, venue="The Quarterly Journal of Economics", type="journal-article",
        doi="10.1093/qje/qjv029", volume="130", issue="4", pages="1623-1667", source="crossref",
    )
    monkeypatch.setattr(resolve, "find_published",
                        lambda *a, **kw: (resolve.Match(fake_pub, 100.0, 1.0, 2, True), "crossref"))

    res = updates.check_all(cfg)
    assert len(res["items"]) == 1
    assert res["items"][0]["work_id"] == wid
    assert res["items"][0]["proposed"]["doi"] == "10.1093/qje/qjv029"

    # Apply upgrade: keeps citekey, saves working_paper, updates meta + keys
    updated = updates.apply(cfg, wid)
    assert updated["citekey"] == "baicker2013behavioral"
    assert updated["meta"]["venue"] == "The Quarterly Journal of Economics"
    assert updated["meta"]["status"] == "confirmed"
    assert "doi:10.1093/qje/qjv029" in updated["keys"]
    assert updated["working_paper"]["year"] == 2013
    assert updates.load(cfg)["items"] == []


def test_batch_import_bibtex_and_syllabus(env):
    cfg, conn, _, _ = env
    _write_read(cfg, "h1", title="Automobile Prices in Market Equilibrium", family="Berry",
                year=1995, doi="10.2307/2171802", venue="Econometrica")
    wid = works.sync(cfg)["h1"]
    scan.scan(cfg, conn)

    bib_text = """
    @article{BLP95,
      author = {Berry, Steven and Levinsohn, James and Pakes, Ariel},
      title = {Automobile Prices in Market Equilibrium},
      journal = {Econometrica},
      year = {1995},
      doi = {10.2307/2171802}
    }
    @article{Nevo2001,
      author = {Nevo, Aviv},
      title = {Measuring Market Power in the Ready-to-Eat Cereal Industry},
      journal = {Econometrica},
      year = {2001},
      doi = {10.1111/1468-0262.00194}
    }
    """
    entries = batch.parse_input(bib_text)
    assert len(entries) == 2

    prev = batch.preview(cfg, conn, entries, resolve_online=False)
    assert prev["counts"]["existing"] == 1
    assert prev["counts"]["new_stub"] == 1

    done = batch.commit(cfg, conn, prev["items"], collection_name="220a", queue=True)
    assert done["tagged"] == 1
    assert done["created"] == 1
    assert "220a" in collection.explicit(works.load_one(cfg, wid))

    # Plain-text syllabus with topic headings
    syllabus = """
    ## Week 1: Demand Estimation
    - Berry, Levinsohn, and Pakes (1995). Automobile Prices in Market Equilibrium. Econometrica.
    - https://arxiv.org/abs/2301.01234
    """
    syl_entries = batch.parse_input(syllabus)
    assert len(syl_entries) == 2
    assert syl_entries[0]["topic"] == "Demand Estimation"
    assert syl_entries[1]["ids"]["arxiv"] == "2301.01234"


def test_web_api_endpoints_and_es_modules(env):
    cfg, conn, root, vault = env
    pdf = root / "refs-library" / "berry-1995-blp.pdf"
    _make_annotated_pdf(pdf, "Demand elasticities for automobiles.", comment="Check Table 4")
    scan.scan(cfg, conn)
    h1 = conn.execute("SELECT hash FROM paths").fetchone()["hash"]
    _write_read(cfg, h1, title="Automobile Prices in Market Equilibrium", family="Berry",
                year=1995, doi="10.2307/2171802", venue="Econometrica")
    scan.scan(cfg, conn)
    [wid] = list(works.load_all(cfg))

    client = TestClient(create_app(watch=False))
    hdr = {"X-Refs": "1"}

    # Static ES modules all serve 200
    for asset in ("/", "/static/app.js", "/static/js/api.js", "/static/js/router.js",
                  "/static/js/workspace.js", "/static/js/updates.js", "/static/js/importer.js",
                  "/static/js/settings.js", "/static/js/onboarding.js", "/static/js/detail_extras.js"):
        assert client.get(asset).status_code == 200

    # 4-state reading + re-read log + queue intent
    assert client.post(f"/api/work/{wid}/read", json={"status": "skimmed", "context": "bets"}, headers=hdr).json()["read_status"] == "skimmed"
    r_log = client.post(f"/api/work/{wid}/reads", json={"status": "read", "context": "bets", "note": "re-read"}, headers=hdr)
    assert r_log.status_code == 200 and len(r_log.json()["reads"]) == 2
    q_res = client.post(f"/api/work/{wid}/queue",
                        json={"on": True, "note": "check IV", "for_collection": "bets", "priority": "next"}, headers=hdr)
    assert q_res.json()["queue_note"] == "check IV" and q_res.json()["queue_priority"] == "next"

    # Personal tags + auto-tag hide
    t_res = client.post(f"/api/work/{wid}/tags", json={"add_user": ["classic"], "hide_auto": ["demand estimation"]}, headers=hdr)
    assert t_res.status_code == 200 and "classic" in t_res.json()["user_tags"]
    assert "demand estimation" not in t_res.json()["tags"]

    # Detail endpoint returns highlights & backlinks; pull highlights & sync note properties
    detail = client.get(f"/api/work/{wid}").json()
    assert len(detail["highlights"]) == 1
    pulled = client.post(f"/api/work/{wid}/highlights/pull", json={"version": ""}, headers=hdr)
    assert pulled.status_code == 200 and "Check Table 4" in pulled.json()["note"]["body"]
    synced = client.post(f"/api/work/{wid}/note/sync", json={}, headers=hdr)
    assert synced.status_code == 200

    # Search includes has_note, marks, read_at, queue_note
    items = client.get("/api/search?q=berry&view=all").json()["items"]
    assert items[0]["has_note"] is True and items[0]["marks"] >= 1 and items[0]["queue_note"] == "check IV"

    # Updates & batch import preview/commit endpoints
    assert client.get("/api/updates").status_code == 200
    prev = client.post("/api/import/preview",
                       json={"text": "@article{N01, author={Nevo, Aviv}, title={Cereal Demand}, year={2001}}",
                             "resolve_online": False}, headers=hdr)
    assert prev.status_code == 200 and prev.json()["counts"]["new_stub"] == 1
    com = client.post("/api/import/commit",
                      json={"items": prev.json()["items"], "collection_name": "220a", "download_oa": False}, headers=hdr)
    assert com.status_code == 200 and com.json()["created"] == 1
    new_wid = com.json()["stubs"][0]["work_id"]

    # Remove stub via API
    rem = client.post(f"/api/work/{new_wid}/remove", json={"trash_files": False}, headers=hdr)
    assert rem.status_code == 200 and works.load_one(cfg, new_wid) is None


def _wait_job(client: TestClient, job_id: str, timeout: float = 5.0) -> dict:
    import time
    deadline = time.time() + timeout
    while time.time() < deadline:
        res = client.get(f"/api/job/{job_id}").json()
        if res.get("state") != "running":
            return res
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} did not finish")


def test_reread_single_batch_and_clear_overrides(env, monkeypatch):
    from refs import llm, pipeline, records

    cfg, conn, root, _ = env
    pdf1 = root / "refs-library" / "chetty-2014.pdf"
    pdf2 = root / "refs-library" / "finkelstein-2012.pdf"
    _make_annotated_pdf(pdf1, "Where is the Land of Opportunity?", comment="Table 1")
    _make_annotated_pdf(pdf2, "The Oregon Health Insurance Experiment.", comment="Table 2")
    scan.scan(cfg, conn)

    hashes = {r["path"]: r["hash"] for r in conn.execute("SELECT path, hash FROM paths").fetchall()}
    h1 = hashes["refs-library/chetty-2014.pdf"]
    h2 = hashes["refs-library/finkelstein-2012.pdf"]

    # Initial poor extraction on both PDFs
    _write_read(cfg, h1, title="Draft Opportunity Paper", family="Chetty", year=2013,
                status="extracted", wp=True, venue=None)
    _write_read(cfg, h2, title="Draft Oregon Paper", family="Finkelstein", year=2011,
                status="extracted", wp=True, venue=None)
    scan.scan(cfg, conn)
    by_file = {h: w["id"] for w in works.load_all(cfg).values() for h in w["files"]}
    w1_id, w2_id = by_file[h1], by_file[h2]
    orig_citekey = works.load_one(cfg, w1_id)["citekey"]

    # Attach user state (collection, reading status, note, and a manual title override) to w1
    collection.change(cfg, [w1_id], "public")
    w1 = works.load_one(cfg, w1_id)
    reading.set_status(w1, "reading", context="public")
    w1["user"]["overrides"] = {"title": "My Custom Title"}
    w1["user"]["confirmed"] = True
    records.write_json(works.works_dir(cfg) / f"{w1_id}.json", w1)
    notes.write(cfg, w1, "Important mobility results.", "")

    # Also add a stub with no PDF
    stub = works.create_stub(cfg, {
        "title": "No PDF Stub", "authors": [{"given": "Ada", "family": "Lovelace"}],
        "year": 2020, "venue": "AER", "type": "journal-article", "doi": "10.1/stub",
        "source": "crossref",
    }, source_url=None)
    scan.scan(cfg, conn)

    seen_calls: list[tuple[str, object, bool]] = []

    def fake_process_file(cfg_, api_, file_hash, rel, topics, *, previous=None, refresh=False):
        seen_calls.append((file_hash, previous, refresh))
        if file_hash == h1:
            rec = _write_read(cfg_, h1, title="Where is the Land of Opportunity?", family="Chetty",
                              year=2014, doi="10.1093/qje/qju022", venue="Quarterly Journal of Economics",
                              status="verified", wp=False)
        else:
            rec = _write_read(cfg_, h2, title="The Oregon Health Insurance Experiment", family="Finkelstein",
                              year=2012, doi="10.1093/qje/qjs020", venue="Quarterly Journal of Economics",
                              status="verified", wp=False)
        return rec

    monkeypatch.setattr(llm, "client", lambda: object())
    monkeypatch.setattr(pipeline, "process_file", fake_process_file)

    client = TestClient(create_app(watch=False))
    hdr = {"X-Refs": "1"}

    # Stub-only re-read fails cleanly with HTTP 400
    bad = client.post("/api/reread", json={"work_ids": [stub["id"]]}, headers=hdr)
    assert bad.status_code == 400

    # Batch re-read (2 PDFs + 1 stub) with clear_overrides=False preserves manual override on w1
    resp = client.post("/api/reread", json={"work_ids": [w1_id, w2_id, stub["id"]], "clear_overrides": False}, headers=hdr)
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 2 and body["skipped_no_pdf"] == 1
    job = _wait_job(client, body["job"])
    assert job["state"] == "done"
    assert "re-read 2 of 2 PDFs (1 had no PDF)" in job["message"]
    assert all(prev is None and ref is True for _, prev, ref in seen_calls)

    w1_after = works.load_one(cfg, w1_id)
    w2_after = works.load_one(cfg, w2_id)
    assert w1_after["citekey"] == orig_citekey
    assert "public" in collection.explicit(w1_after)
    assert w1_after["user"]["read_status"] == "reading"
    assert notes.read(cfg, w1_after)["body"].strip() == "Important mobility results."
    # Manual title override kept, but venue + DOI updated from fresh scan
    assert w1_after["meta"]["title"] == "My Custom Title"
    assert w1_after["meta"]["venue"] == "Quarterly Journal of Economics"
    assert w1_after["meta"]["doi"] == "10.1093/qje/qju022"
    assert w2_after["meta"]["title"] == "The Oregon Health Insurance Experiment"
    assert w2_after["meta"]["status"] == "verified"

    # Single re-read with clear_overrides=True clears manual title override
    resp2 = client.post("/api/reread", json={"work_ids": [w1_id], "clear_overrides": True}, headers=hdr)
    assert resp2.status_code == 200
    job2 = _wait_job(client, resp2.json()["job"])
    assert job2["state"] == "done"
    w1_cleared = works.load_one(cfg, w1_id)
    assert "overrides" not in w1_cleared["user"]
    assert w1_cleared["meta"]["title"] == "Where is the Land of Opportunity?"
    assert w1_cleared["meta"]["status"] == "verified"
    assert w1_cleared["citekey"] == orig_citekey


