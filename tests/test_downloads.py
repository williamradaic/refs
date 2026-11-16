"""PDFs in ~/Downloads: which look like papers, and taking them into the library.

Runs against a fake home folder (with its own Downloads) and a fake Dropbox.
`refs add`, which the server runs to read what was taken in, is replaced by a
stand-in that records its arguments: nothing is read or paid for.
"""

import os
import shutil
import time

import pymupdf
import pytest
from fastapi.testclient import TestClient

from refs import config, downloads, index, scan, settings
from refs.web import app as web_app
from refs.web.app import create_app

H = {"X-Refs": "1"}
PROJECT = "phd/research/bets"


def make_pdf(path, pages):
    """A PDF whose pages carry the given text (a list, one string per page)."""
    doc = pymupdf.open()
    for text in pages:
        page = doc.new_page()
        page.insert_textbox(pymupdf.Rect(50, 50, 550, 800), text, fontsize=9)
    doc.save(path)
    doc.close()


FILLER = ("We study how households respond to prices in differentiated product markets "
          "and estimate demand with aggregate data on market shares. ") * 6
PAPER = [
    "Automobile Prices in Market Equilibrium. Steven Berry, James Levinsohn, Ariel Pakes. "
    "Abstract: We develop techniques for empirically analyzing demand and supply in "
    "differentiated products markets. JEL Codes: L11, L62. Keywords: demand estimation. "
    "Econometrica, Vol. 63. doi:10.2307/2171802 " + FILLER,
    *[FILLER + " As in Berry (1994) and Berry et al. (1995), see (Pakes, 1986)." for _ in range(8)],
    "References. Berry, S. (1994). Estimating discrete-choice models. Pakes, A. (1986). "
    "Patents as options. Hausman, J. (1996). Valuation of new goods. " + FILLER,
]
# A public-finance paper: full of "tax" and "payment", which must not count against it.
TAX_PAPER = [
    "Optimal Income Taxation with Behavioral Agents. Working Paper. Department of Economics. "
    "Abstract: Tax payments, tax rates and transfer payments are studied in a model where "
    "agents misperceive the tax schedule. " + FILLER,
    *[FILLER + " The tax payment and the account of Saez (2001) and Chetty et al. (2009)." for _ in range(6)],
    "Bibliography. Saez, E. (2001). Using elasticities. Chetty, R. (2009). Salience and taxation.",
]
RECEIPT = ["ACME Store. Invoice number: 10442. Order number 99812. Billing address: 1 Main St. "
           "Subtotal $40.00. Sales tax $3.60. Amount due $43.60. Thank you for your order."]
TICKET = ["Boarding pass. Passenger: J. Doe. Confirmation code QX7P2. Gate 12, seat 14C.",
          "Itinerary: SFO to GRU. Booking reference QX7P2."]


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A fake home with Downloads, a fake Dropbox holding one paper, and `refs add` stubbed."""
    root = tmp_path / "Dropbox"
    (root / "papers-dump").mkdir(parents=True)
    (root / PROJECT / "overleaf").mkdir(parents=True)
    dl = tmp_path / "home" / "Downloads"
    dl.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("REFS_DROPBOX_ROOT", str(root))
    monkeypatch.setenv("REFS_INDEX_DB", str(tmp_path / "index.db"))
    monkeypatch.setenv("REFS_CONFIG", str(tmp_path / "none.toml"))
    make_pdf(root / PROJECT / "blp.pdf", PAPER)                 # already in the library
    cfg = config.load()
    scan.scan(cfg, index.connect(cfg))
    downloads._seen.clear()

    # Downloads: two papers, two pieces of paperwork, a copy of the library's
    # paper, and a file still being written. Modification times give the order.
    make_pdf(dl / "w31234.pdf", TAX_PAPER)
    make_pdf(dl / "receipt.pdf", RECEIPT)
    make_pdf(dl / "boarding-pass.pdf", TICKET)
    make_pdf(dl / "demand.pdf", PAPER[:1] + PAPER[1:4] + PAPER[-1:])
    shutil.copy2(root / PROJECT / "blp.pdf", dl / "blp (1).pdf")
    (dl / "partial.pdf").write_bytes(b"%PDF-1.7\n1 0 obj")
    for n, name in enumerate(["w31234.pdf", "receipt.pdf", "boarding-pass.pdf", "demand.pdf",
                              "blp (1).pdf", "partial.pdf"]):
        os.utime(dl / name, (1_700_000_000 + n, 1_700_000_000 + n))

    calls = []

    class Done:
        returncode, stdout, stderr = 0, "read", ""

    def fake_run(args, **_kw):
        calls.append(args)
        return Done()
    monkeypatch.setattr(web_app.subprocess, "run", fake_run)

    class Home:
        pass
    home = Home()
    home.root, home.dl, home.calls = root, dl, calls
    home.client = TestClient(create_app())
    return home


def wait(client, job):
    for _ in range(100):
        found = client.get(f"/api/job/{job}").json()
        if found["state"] != "running":
            return found
        time.sleep(0.02)
    raise AssertionError("the job never finished")


# --- which PDFs are papers ---------------------------------------------------------------

def test_papers_and_paperwork_are_told_apart(home):
    kinds = {name: downloads.judge(home.dl / name).kind
             for name in ["w31234.pdf", "demand.pdf", "receipt.pdf", "boarding-pass.pdf", "partial.pdf"]}
    assert kinds == {"w31234.pdf": "paper", "demand.pdf": "paper", "receipt.pdf": "other",
                     "boarding-pass.pdf": "other", "partial.pdf": "incomplete"}


def test_cvs_forms_and_unlabelled_journal_articles(home):
    # A CV cites papers and has a references section, but is not a paper.
    make_pdf(home.dl / "jane-doe.pdf", [
        "Jane Doe. Curriculum Vitae. Research Interests: public finance. Citizenship: Brazil. "
        "Publications: Doe (2021), Doe and Roe (2023), Doe et al. (2024), Roe (2019), Doe (2018).",
        "References: Prof. A (a@b.edu), Prof. B (b@c.edu). University of California."])
    # Long, from a university, but with nothing of a paper's: a form.
    make_pdf(home.dl / "application.pdf",
             ["University of California application form. Name, address, program. " * 20] * 9)
    # A journal that prints no "Abstract" and cites in Chicago style.
    make_pdf(home.dl / "670401.pdf", [
        "Pass-Through as an Economic Tool. E. Glen Weyl, University of Chicago. We extend five "
        "principles of tax incidence to imperfect competition. " + FILLER,
        *[FILLER] * 10,
        "References. Weyl, E. Glen. 2012. “Market Power Screens.” Q.J.E. Tirole, Jean. 1988. “The "
        "Theory.” MIT Press. Bulow, J. 1983. “Pass-Through.” JPE. Fabinger, M. 2013. “Notes.” "
        "Manuscript. Pakes, A. 1986. “Patents.” Econometrica."])
    assert downloads.judge(home.dl / "jane-doe.pdf").kind == "other"
    assert downloads.judge(home.dl / "application.pdf").kind == "other"
    assert downloads.judge(home.dl / "670401.pdf").kind == "paper"


def test_a_scan_with_a_paper_name_is_a_maybe(home):
    doc = pymupdf.open()
    for _ in range(12):
        doc.new_page()                               # no text layer, like a scan
    doc.save(home.dl / "Berry 1994 Estimating discrete choice.pdf")
    doc.save(home.dl / "scan0001.pdf")
    assert downloads.judge(home.dl / "Berry 1994 Estimating discrete choice.pdf").kind == "maybe"
    assert downloads.judge(home.dl / "scan0001.pdf").kind == "maybe"      # long: could be one


def test_the_list_is_newest_first_and_knows_what_refs_has(home):
    data = home.client.get("/api/downloads").json()
    assert data["folder"] == str(home.dl.resolve())
    names = [i["name"] for i in data["items"]]
    assert names == ["partial.pdf", "blp (1).pdf", "demand.pdf", "boarding-pass.pdf",
                     "receipt.pdf", "w31234.pdf"]
    by_name = {i["name"]: i for i in data["items"]}
    assert by_name["blp (1).pdf"]["in_refs"] == f"{PROJECT}/blp.pdf"
    assert by_name["demand.pdf"]["in_refs"] is None
    assert by_name["receipt.pdf"]["kind"] == "other"


def test_subfolders_links_and_other_files_are_not_listed(home, tmp_path):
    (home.dl / "sub").mkdir()
    make_pdf(home.dl / "sub" / "inner.pdf", PAPER)
    make_pdf(home.dl / ".hidden.pdf", PAPER)
    (home.dl / "notes.txt").write_text("not a pdf")
    make_pdf(tmp_path / "elsewhere.pdf", PAPER)
    (home.dl / "link.pdf").symlink_to(tmp_path / "elsewhere.pdf")
    names = {i["name"] for i in home.client.get("/api/downloads").json()["items"]}
    assert not names & {"inner.pdf", ".hidden.pdf", "notes.txt", "link.pdf"}


def test_the_sidebar_counts_papers_not_in_refs(home):
    first = home.client.get("/api/sidebar").json()["presets"]["downloads"]
    assert first == 0                         # nothing looked at yet: looked at in the background
    downloads.warm(config.load())
    assert home.client.get("/api/sidebar").json()["presets"]["downloads"] == 2    # w31234, demand


# --- taking them in ----------------------------------------------------------------------

def test_take_into_the_dump_moves_reads_and_skips_known(home):
    result = home.client.post("/api/downloads/take", headers=H, json={
        "names": ["demand.pdf", "w31234.pdf", "blp (1).pdf"]}).json()
    assert result["folder"] == "papers-dump"
    assert [t["to"] for t in result["taken"]] == ["papers-dump/demand.pdf", "papers-dump/w31234.pdf"]
    assert result["known"] == [{"name": "blp (1).pdf", "path": f"{PROJECT}/blp.pdf"}]
    assert not (home.dl / "demand.pdf").exists() and (home.root / "papers-dump/demand.pdf").is_file()
    assert (home.dl / "blp (1).pdf").is_file()                   # left where it was
    # One job reads both, through `refs add <path>`, one after the other.
    assert wait(home.client, result["job"])["message"].startswith("read 2 of 2")
    assert [c[3:5] for c in home.calls] == [["add", str(home.root / "papers-dump/demand.pdf")],
                                            ["add", str(home.root / "papers-dump/w31234.pdf")]]


def test_copy_into_a_new_project_folder_keeps_the_original(home):
    result = home.client.post("/api/downloads/take", headers=H, json={
        "names": ["demand.pdf"], "folder": f"{PROJECT}/lit", "create": True, "mode": "copy",
        "queue": True}).json()
    assert result["taken"] == [{"name": "demand.pdf", "to": f"{PROJECT}/lit/demand.pdf"}]
    assert (home.dl / "demand.pdf").is_file() and (home.root / PROJECT / "lit/demand.pdf").is_file()
    wait(home.client, result["job"])
    assert home.calls[0][3:] == ["add", str(home.root / PROJECT / "lit/demand.pdf"), "--queue"]


def test_nothing_is_overwritten(home):
    (home.root / "papers-dump/demand.pdf").write_bytes(b"%PDF- someone else's file %%EOF")
    result = home.client.post("/api/downloads/take", headers=H, json={"names": ["demand.pdf"]}).json()
    assert result["taken"][0]["to"] == "papers-dump/demand-2.pdf"
    assert (home.root / "papers-dump/demand.pdf").read_bytes() == b"%PDF- someone else's file %%EOF"


def test_two_identical_downloads_are_taken_once(home):
    shutil.copy2(home.dl / "demand.pdf", home.dl / "demand (1).pdf")
    result = home.client.post("/api/downloads/take", headers=H, json={
        "names": ["demand.pdf", "demand (1).pdf"]}).json()
    assert [t["name"] for t in result["taken"]] == ["demand.pdf"]
    assert result["known"] == [{"name": "demand (1).pdf", "path": "the same file as demand.pdf"}]
    assert (home.dl / "demand (1).pdf").is_file()


def test_only_pdfs_directly_in_downloads_can_be_named(home, tmp_path):
    make_pdf(tmp_path / "secret.pdf", PAPER)
    (home.dl / "sub").mkdir()
    make_pdf(home.dl / "sub" / "inner.pdf", PAPER)
    (home.dl / "link.pdf").symlink_to(tmp_path / "secret.pdf")
    names = ["../secret.pdf", "sub/inner.pdf", "link.pdf", ".hidden.pdf", "notes.txt",
             "missing.pdf", "partial.pdf", str(tmp_path / "secret.pdf")]
    result = home.client.post("/api/downloads/take", headers=H, json={"names": names}).json()
    assert result["taken"] == [] and result["job"] is None
    assert {r["name"] for r in result["refused"]} == set(names)
    assert (tmp_path / "secret.pdf").is_file() and (home.dl / "sub/inner.pdf").is_file()


@pytest.mark.parametrize("folder", ["elsewhere", f"{PROJECT}/overleaf", "../outside", "phd/tools/refs"])
def test_only_library_folders_receive_them(home, folder):
    response = home.client.post("/api/downloads/take", headers=H, json={
        "names": ["demand.pdf"], "folder": folder})
    assert response.status_code == 409
    assert (home.dl / "demand.pdf").is_file()


def test_changes_need_the_page_header(home):
    response = home.client.post("/api/downloads/take", json={"names": ["demand.pdf"]})
    assert response.status_code == 403
    assert (home.dl / "demand.pdf").is_file()


# --- the setting -------------------------------------------------------------------------

def test_an_empty_setting_turns_it_off(home, tmp_path):
    (tmp_path / "none.toml").write_text('downloads_folder = ""\n')
    client = TestClient(create_app())
    assert client.get("/api/sidebar").json()["presets"]["downloads"] is None
    assert client.get("/api/downloads").json()["items"] == []
    assert client.post("/api/downloads/take", headers=H, json={"names": ["demand.pdf"]}).status_code == 409


def test_the_setting_must_be_a_folder_outside_the_library(home, tmp_path):
    other = tmp_path / "Saved"
    other.mkdir()
    assert settings.save({"downloads_folder": str(other)}).downloads_folder == other.resolve()
    with pytest.raises(settings.SettingError, match="not a folder"):
        settings.save({"downloads_folder": str(tmp_path / "nope")})
    with pytest.raises(settings.SettingError, match="inside the library"):
        settings.save({"downloads_folder": str(home.root / "papers-dump")})
    assert settings.save({"downloads_folder": ""}).downloads_folder is None
