"""The bookmarklet's way in (/api/capture) and reading addresses (capture.py).

`refs add` itself is not run: the server's subprocess call is replaced by one
that records the command it would have run.
"""

import base64
import time

import pytest
from fastapi.testclient import TestClient

from refs import capture, config, index, scan, settings
from refs.web import app as web

H = {"X-Refs": "1"}


@pytest.fixture
def server(tmp_path, monkeypatch):
    root = tmp_path / "Dropbox"
    (root / "papers-dump").mkdir(parents=True)
    (root / "phd").mkdir()
    monkeypatch.setenv("REFS_DROPBOX_ROOT", str(root))
    monkeypatch.setenv("REFS_INDEX_DB", str(tmp_path / "index.db"))
    ran = []

    class Done:
        returncode, stdout, stderr = 0, "Added it", ""

    monkeypatch.setattr(web.subprocess, "run", lambda command, **_: ran.append(command) or Done())
    client = TestClient(web.create_app())
    client.root, client.ran, client.cfg = root, ran, config.load()
    return client


def wait(client, job_id):
    for _ in range(100):
        job = client.get(f"/api/job/{job_id}").json()
        if job["state"] != "running":
            return job
        time.sleep(0.01)
    raise AssertionError("the job never ended")


def token(client):
    return client.get("/api/bookmarklet").json()["token"]


def test_the_key_is_stable_and_required(server):
    key = token(server)
    assert len(key) >= 20 and token(server) == key
    r = server.post("/api/capture", headers=H, json={"token": "wrong", "url": "https://x.org/a"})
    assert r.status_code == 403 and "out of date" in r.json()["detail"]
    assert server.post("/api/capture", json={"token": key, "url": "https://x.org/a"}).status_code == 403
    assert server.ran == []


def test_a_pdf_from_the_browser_is_saved_and_added_with_the_pages_doi(server):
    pdf = b"%PDF-1.7 a paper"
    r = server.post("/api/capture", headers=H, json={
        "token": token(server), "url": "https://academic.oup.com/qje/article/1/2/3",
        "meta": {"doi": "10.1093/qje/qjad047", "title": "A Paper",
                 "pdf_url": "https://academic.oup.com/x.pdf"},
        "pdf": base64.b64encode(pdf).decode(), "queue": True})
    assert wait(server, r.json()["job"])["state"] == "done"
    [command] = server.ran
    saved = command[command.index("add") + 1]
    assert saved.startswith(str(server.root / "papers-dump" / "added-")) and open(saved, "rb").read() == pdf
    assert command[command.index("--doi") + 1] == "10.1093/qje/qjad047"
    assert command[command.index("--source-url") + 1] == "https://academic.oup.com/qje/article/1/2/3"
    assert "--queue" in command and "--pdf-url" not in command     # the PDF is already here


def test_a_known_pdf_is_not_saved_twice(server):
    (server.root / "phd/known.pdf").write_bytes(b"%PDF known")
    conn = index.connect(server.cfg)
    scan.scan(server.cfg, conn)
    conn.execute("UPDATE files SET status='verified'")
    conn.commit()
    r = server.post("/api/capture", headers=H, json={
        "token": token(server), "url": "https://x.org/p", "pdf": base64.b64encode(b"%PDF known").decode()})
    assert "Already in the library" in r.json()["message"] and server.ran == []


def test_without_a_pdf_refs_tries_the_link_and_never_refetches_the_page(server):
    r = server.post("/api/capture", headers=H, json={
        "token": token(server), "url": "https://www.aeaweb.org/articles?id=10.1257/aer.20220276",
        "meta": {"doi": "10.1257/aer.20220276", "pdf_url": "https://www.aeaweb.org/articles/pdf/doi/x"}})
    wait(server, r.json()["job"])
    [command] = server.ran
    assert command[command.index("--pdf-url") + 1] == "https://www.aeaweb.org/articles/pdf/doi/x"
    assert "--no-page" in command


def test_not_a_pdf_is_refused(server):
    r = server.post("/api/capture", headers=H, json={
        "token": token(server), "url": "https://x.org/p", "pdf": base64.b64encode(b"<html>").decode()})
    assert r.status_code == 400 and server.ran == []


def test_the_capture_and_bookmarklet_pages_are_served(server):
    assert "refs-capture" in server.get("/capture").text
    assert "grab" in server.get("/bookmarklet").text


@pytest.mark.parametrize("thing, ids, pdfs", [
    ("https://www.nber.org/papers/w32515", {"nber": "32515"}, []),
    ("https://www.nber.org/system/files/working_papers/w32515/w32515.pdf", {"nber": "32515"},
     ["https://www.nber.org/system/files/working_papers/w32515/w32515.pdf"]),
    ("10.3386/w32515", {"nber": "32515"}, []),
    ("https://www.aeaweb.org/articles?id=10.1257/aer.20220276", {"doi": "10.1257/aer.20220276"}, []),
    ("https://academic.oup.com/qje/article/139/1/1/7272953", {}, []),
])
def test_addresses_are_read(thing, ids, pdfs):
    target = capture.parse(thing)
    assert target.ids == ids and target.pdf_urls == pdfs


def test_the_pages_own_doi_and_pdf_link_come_first():
    target = capture.hinted(capture.parse("https://www.nber.org/system/files/w1.pdf"),
                            doi="https://doi.org/10.3386/w32515", pdf_url="https://www.nber.org/b.pdf")
    assert target.ids == {"doi": "10.3386/w32515", "nber": "32515"}
    assert target.pdf_urls == ["https://www.nber.org/b.pdf", "https://www.nber.org/system/files/w1.pdf"]
    # Not a DOI (some pages put an ISSN or a URL in dc.identifier): ignored.
    assert capture.hinted(capture.Target(), doi="ISSN 0002-8282").ids == {}


def test_meta_tags_in_either_attribute_order():
    found = capture.page_meta('<META content="10.1/a" name="citation_doi">'
                              "<meta name='citation_author' content='Doe, J'><meta name=x>"
                              '<meta property="og:title" content="T">')
    assert found == {"citation_doi": ["10.1/a"], "citation_author": ["Doe, J"], "og:title": ["T"]}


def test_the_library_proxy_setting_is_checked():
    assert settings.normalize("library_proxy", " https://libproxy.berkeley.edu/login?qurl= ", None) \
        == "https://libproxy.berkeley.edu/login?qurl="
    assert settings.normalize("library_proxy", "", None) == ""
    with pytest.raises(settings.SettingError):
        settings.normalize("library_proxy", "libproxy.berkeley.edu", None)


def test_direct_capture_upload_cors_prefs_and_cid_dedup(server):
    key = token(server)
    # Sync capture preferences (queue + collection) from /capture
    pref = server.post("/api/capture-prefs", headers=H, json={"queue": True, "collections": ["bets"]})
    assert pref.status_code == 200

    # Preflight CORS + Private Network Access headers
    pre = server.options("/api/capture-upload", headers={"Origin": "https://oup.silverchair-cdn.com"})
    assert pre.status_code == 204
    assert pre.headers["access-control-allow-origin"] == "https://oup.silverchair-cdn.com"
    assert pre.headers["access-control-allow-private-network"] == "true"

    # Wrong token is rejected even from a publisher origin
    bad = server.post("/api/capture-upload?cid=c1", content=b"%PDF-1.7 x",
                      headers={"Origin": "https://oup.silverchair-cdn.com", "X-Refs-Token": "wrong"})
    assert bad.status_code == 403 and server.ran == []

    # Valid cross-origin direct upload (with leading whitespace before %PDF-1.7) succeeds and uses saved prefs
    pdf = b"\n  %PDF-1.7 direct bytes"
    ok = server.post(
        "/api/capture-upload?cid=cid123&url=https://oup.silverchair-cdn.com/x.pdf&doi=10.1093/qje/qjad047",
        content=pdf,
        headers={"Origin": "https://oup.silverchair-cdn.com", "X-Refs-Token": key},
    )
    assert ok.status_code == 200
    assert ok.headers["access-control-allow-origin"] == "https://oup.silverchair-cdn.com"
    job_id = ok.json()["job"]
    assert wait(server, job_id)["state"] == "done"
    assert len(server.ran) == 1
    cmd = server.ran[0]
    assert "--queue" in cmd and "--to" in cmd and "bets" in cmd

    # Duplicate upload with same cid is deduplicated (does not run refs add twice)
    dup = server.post(
        "/api/capture-upload?cid=cid123&url=https://oup.silverchair-cdn.com/x.pdf",
        content=pdf,
        headers={"Origin": "https://oup.silverchair-cdn.com", "X-Refs-Token": key},
    )
    assert dup.status_code == 200 and dup.json()["job"] == job_id and len(server.ran) == 1

    # Rendezvous status works both same-origin and cross-origin with token
    st = server.get("/api/capture-status/cid123")
    assert st.status_code == 200 and st.json()["found"] is True and st.json()["job"] == job_id
    st_cors = server.get(f"/api/capture-status/cid123?token={key}",
                         headers={"Origin": "https://oup.silverchair-cdn.com"})
    assert st_cors.status_code == 200 and st_cors.json()["found"] is True


def test_raw_capture_and_multipart_form_fallback(server):
    key = token(server)
    # Channel 2: same-origin raw binary upload from capture.html
    r_raw = server.post(
        "/api/capture-raw?cid=raw1&url=https://example.org/p.pdf&to=220a&queue=true",
        content=b"%PDF-1.7 raw upload",
        headers={**H, "X-Refs-Token": key},
    )
    assert r_raw.status_code == 200
    assert wait(server, r_raw.json()["job"])["state"] == "done"
    assert "220a" in server.ran[0] and "--queue" in server.ran[0]

    # Channel 3: multipart <form> upload fallback (defeats COOP + connect-src CSP)
    r_form = server.post(
        "/capture-form",
        data={"token": key, "cid": "form1", "url": "https://wiley.com/doi/pdf/10.3982/ECTA1",
              "doi": "10.3982/ECTA1", "title": "Econometrica Paper"},
        files={"pdf": ("paper.pdf", b"%PDF-1.7 form fallback", "application/pdf")},
        headers={"Origin": "https://onlinelibrary.wiley.com"},
        follow_redirects=False,
    )
    assert r_form.status_code == 303
    assert r_form.headers["location"] == "/capture?cid=form1"
    st = server.get("/api/capture-status/form1").json()
    assert st["found"] is True
    assert wait(server, st["job"])["state"] == "done"
    assert len(server.ran) == 2

