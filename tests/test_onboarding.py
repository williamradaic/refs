"""First run: the folder survey, suggestions, `refs init` and the page's API (onboarding.py)."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from refs import cli, config, onboarding
from refs.web.app import create_app

pytestmark = pytest.mark.generic_defaults

FILES = [
    "Research/literature/io/berry-1994.pdf",
    "Research/literature/labor/card-1994.pdf",
    "Research/referee/journal-x/manuscript.pdf",
    "Research/readings/03-tax-design-insurance/saez-2001.pdf",
    "Classes/Fall 2026/Econ 240A/readings/01-intro/smith-2020.pdf",
    "Classes/Spring 2026/Econ 201B/week1.pdf",
    "Personal/visa/i-20.pdf",
    "Personal/O'DR 2006 Sin Taxes/odr.pdf",
    "Research/.git/objects/x.pdf",              # hidden: skipped
    "Research/node_modules/pkg/doc.pdf",         # ignored name: skipped
]


@pytest.fixture
def library(tmp_path, monkeypatch):
    root = tmp_path / "Dropbox"
    for rel in FILES:
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_bytes(b"%PDF-1.4 test\n%%EOF")
    (root / "Empty").mkdir()
    monkeypatch.setenv("REFS_DROPBOX_ROOT", str(root))
    monkeypatch.setenv("REFS_INDEX_DB", str(tmp_path / "index.db"))
    monkeypatch.setenv("REFS_CONFIG", str(tmp_path / "config.toml"))
    return root.resolve()


def test_survey_counts_pdfs_per_folder(library):
    found = onboarding.survey(library)
    tree = found["tree"]
    by_name = {c["name"]: c for c in tree["children"]}
    assert tree["pdfs"] == 8 and found["complete"]
    assert (by_name["Research"]["pdfs"], by_name["Classes"]["pdfs"], by_name["Empty"]["pdfs"]) == (4, 2, 0)
    literature = next(c for c in by_name["Research"]["children"] if c["name"] == "literature")
    assert literature["pdfs"] == 2 and {c["name"] for c in literature["children"]} == {"io", "labor"}
    assert onboarding.pdfs_in(tree, ["Research", "Classes"], exclude=["Research/referee"]) == 6 - 0
    assert onboarding.pdfs_in(tree, ["Research/literature"]) == 2


def test_private_folders_are_suggested_by_short_names_only(library):
    found = {s["path"]: s for s in onboarding.survey(library)["suggestions"]}
    assert found["Personal/visa"] == {"path": "Personal/visa", "kind": "exclude", "ticked": True}
    assert found["Research/referee"]["kind"] == "no_llm" and found["Research/referee"]["ticked"]
    # A paper's folder and a readings topic are not paperwork.
    assert "Personal/O'DR 2006 Sin Taxes" not in found
    assert not any("tax-design" in path for path in found)


def test_term_folders_suggest_a_course_rule(library):
    found = onboarding.survey(library)
    assert found["term_rules"] == ["Classes/{term}/{course}"]
    assert onboarding.rules_for(found, ["Classes"]) == ["Classes/{term}/{course}"]
    assert onboarding.rules_for(found, ["Research"]) == []


def test_library_candidates_and_starting_without_dropbox(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / "Library" / "Mobile Documents" / "com~apple~CloudDocs").mkdir(parents=True)
    (home / "Documents").mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("REFS_DROPBOX_ROOT", raising=False)
    monkeypatch.setenv("REFS_CONFIG", str(tmp_path / "none.toml"))
    assert [c["label"] for c in onboarding.library_candidates()] == ["iCloud Drive", "Documents"]
    # No Dropbox and nothing watched: refs starts (onboarding can ask) ...
    assert config.load().dropbox_root == home.resolve()
    # ... but with watched folders, a missing library is an error, never a guess.
    (tmp_path / "none.toml").write_text('roots = ["papers"]\n')
    with pytest.raises(SystemExit, match="Could not find the library folder"):
        config.load()


def test_init_in_the_terminal(library, monkeypatch, capsys):
    answers = iter(["1", "Research Classes", "", "", "", "", "none", "n"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    monkeypatch.setattr("refs.settings.api_key_status", lambda: "set")
    assert cli.main(["init"]) == 0
    cfg = config.load()
    assert cfg.roots == ["Research", "Classes", "papers-dump"]
    assert (library / "papers-dump").is_dir()
    assert cfg.no_llm == ["Research/referee"] and cfg.auto_process == "none"
    assert cfg.contexts == ["Classes/{term}/{course}"] and cfg.obsidian_vault is None
    assert "background service was left alone" in capsys.readouterr().out
    # Set up now: init says so instead of asking again.
    assert cli.main(["init"]) == 0
    assert "already set up" in capsys.readouterr().out


def test_page_api_for_the_first_run(library):
    client = TestClient(create_app())
    info = client.get("/api/onboarding").json()
    assert info["needed"] and info["libraries"][0]["path"] == str(library)
    survey = client.get("/api/onboarding/survey", params={"root": str(library)}).json()
    assert survey["tree"]["pdfs"] == 8
    missing = client.get("/api/onboarding/survey", params={"root": str(library / "nope")})
    assert missing.status_code == 400
    # The answers are saved like any settings change; then refs is set up.
    saved = client.post("/api/settings", headers={"X-Refs": "1"},
                        json={"changes": {"roots": ["Research"], "auto_process": "none"}})
    assert saved.status_code == 200
    assert not client.get("/api/onboarding").json()["needed"]


def test_watcher_reports_progress_during_a_pass(library):
    from refs.watch import Watcher
    (library.parent / "config.toml").write_text('roots = ["Research"]\nauto_process = "none"\n')
    watcher = Watcher(config.load(), notifier=None)
    seen = []
    original = watcher._scanning
    watcher._scanning = lambda done, total: (original(done, total), seen.append(dict(watcher.progress)))
    watcher.run_pass()
    assert seen and seen[-1]["stage"] == "indexing" and seen[0]["total"] == 4
    assert watcher.status()["progress"] is None          # over: no progress left
