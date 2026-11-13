"""Semantic search, notes in the index, and asking Haiku.

(embed.py, query.py, ask.py and the hybrid ranking in index.search.) The
embedding model is the fake one from conftest.py, which treats "frozen
desserts" and "ice cream" as the same thing; Haiku is a fake client that
records what it was sent.
"""

import dataclasses
import json
from types import SimpleNamespace

import pytest

from refs import ask, config, index, query, records, scan

ICE = "papers-dump/sullivan-2020-split-apart.pdf"
BLP = "phd/coursework/2026-fall/220a/readings/03-demand/berry-1995-automobile-prices.pdf"
SALIENCE = "phd/coursework/2026-fall/219a/readings/01-attention/chetty-2009-salience.pdf"
REFEREE = "phd/referee/econometrica-2026/submission.pdf"


@pytest.fixture
def lib(tmp_path, monkeypatch):
    """A fake Dropbox with three read papers (and one confidential file), scanned."""
    root = tmp_path / "Dropbox"
    monkeypatch.setenv("REFS_DROPBOX_ROOT", str(root))
    monkeypatch.setenv("REFS_INDEX_DB", str(tmp_path / "index.db"))
    monkeypatch.setenv("REFS_CONFIG", str(tmp_path / "none.toml"))
    (root / "papers-dump").mkdir(parents=True)
    (tmp_path / "test-vault" / ".obsidian").mkdir(parents=True)

    papers = {
        ICE: ("Split Apart: Differentiation and Coordination", "Sullivan", 2020,
              "The paper studies the superpremium ice cream market and how two brands "
              "coordinate flavors.", ["industrial organization", "product differentiation"]),
        BLP: ("Automobile Prices in Market Equilibrium", "Berry", 1995,
              "The paper estimates demand for automobiles with random coefficients.",
              ["industrial organization", "demand estimation"]),
        SALIENCE: ("Salience and Taxation", "Chetty", 2009,
                   "The paper shows that consumers underreact to taxes not shown in posted prices.",
                   ["public economics", "behavioral economics"]),
        REFEREE: ("A Secret Submission About Gelato", "Anonymous", 2026,
                  "Confidential manuscript on gelato pricing.", ["industrial organization"]),
    }
    for n, rel in enumerate(papers):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_bytes(f"pdf {n}".encode())
    cfg = config.load()
    conn = index.connect(cfg)
    scan.scan(cfg, conn)

    hashes = {r["path"]: r["hash"] for r in conn.execute("SELECT path, hash FROM paths")}
    for rel, (title, family, year, description, tags) in papers.items():
        reading = {"kind": "paper", "title": title, "year": year, "venue": None,
                   "authors": [{"given": "A.", "family": family}], "is_working_paper": False,
                   "description": description, "fields": tags, "methods": [], "topics": []}
        status = "confidential" if rel == REFEREE else "extracted"
        records.write_json(records.reads_dir(cfg) / f"{hashes[rel]}.json",
                           {"status": status, "issues": [], "reading": reading, "registry": None})
    scan.scan(cfg, conn)
    return SimpleNamespace(cfg=cfg, conn=conn, root=root, hashes=hashes)


def titles(results):
    return [r["title"] for r in results]


# --- Hybrid ranking ------------------------------------------------------------------

@pytest.fixture
def lenient(monkeypatch):
    """The fake model's similarities are lower than the real model's: relax the cut-off."""
    monkeypatch.setattr(index, "SEMANTIC_FLOOR", 0.2)


def test_meaning_finds_a_paper_with_no_word_in_common(lib, lenient):
    words = ["frozen", "desserts", "brands"]
    assert index.search(lib.conn, words, semantic=False) == []          # keywords alone: nothing
    found = index.search(lib.conn, words, semantic=True)
    assert titles(found)[0] == "Split Apart: Differentiation and Coordination"


def test_a_lookup_stays_a_keyword_search(lib, lenient):
    report: dict = {}
    found = index.search(lib.conn, ["berry"], semantic=True, report=report)
    assert titles(found) == ["Automobile Prices in Market Equilibrium"]
    assert report["semantic_used"] is False


def test_abbreviation_matches_its_expansion(lib):
    # "IO" is not in any text, but the tag "industrial organization" is.
    found = index.search(lib.conn, ["IO", "automobile"])
    assert titles(found) == ["Automobile Prices in Market Equilibrium"]


def test_only_changed_texts_are_embedded_again(lib):
    assert scan.scan(lib.cfg, lib.conn).embedded == 0
    other = dataclasses.replace(lib.cfg, embedding_model="another/model")
    assert scan.scan(other, lib.conn).embedded == 4          # new model: everything again


# --- Notes ------------------------------------------------------------------------

def test_notes_are_found_by_word_and_by_meaning(lib, lenient):
    citekey = lib.conn.execute("SELECT citekey FROM files WHERE hash=?",
                               (lib.hashes[BLP],)).fetchone()["citekey"]
    note = lib.cfg.obsidian_vault / lib.cfg.obsidian_folder / f"{citekey}.md"
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text("---\ncitekey: x\n---\nNested logit with gelato as the outside good.\n")

    assert scan.scan(lib.cfg, lib.conn).embedded == 1        # just the new note piece
    assert titles(index.search(lib.conn, ["nested", "logit"])) == [
        "Automobile Prices in Market Equilibrium"]
    # The front matter is not indexed.
    assert index.search(lib.conn, ["citekey"]) == []


# --- Query shape ---------------------------------------------------------------------

@pytest.mark.parametrize("text, expected", [
    ("IO paper on ice cream", True),
    ("salience tax experiment", True),
    ("the one where people misperceive tax schedules", True),
    ("blp", False),
    ("chetty 2009", False),
    ("present bias", False),
    ("odonoghue2006optimal", False),
    ("?", False),
])
def test_sentence_like(text, expected):
    assert query.sentence_like(text.split(), {"chetty"}) is expected


def test_ambiguous_abbreviations_need_capitals():
    assert query.abbreviation("io") == "industrial organization"
    assert query.abbreviation("did") is None
    assert query.abbreviation("DiD") == "difference-in-differences"


# --- Asking Haiku -----------------------------------------------------------------------

class FakeAPI:
    """Stands in for anthropic.Anthropic: answers with the given picks, records prompts."""

    def __init__(self, picks):
        self.picks, self.prompts = picks, []
        self.messages = self

    def parse(self, **request):
        self.prompts.append(request["messages"][0]["content"])
        return SimpleNamespace(
            usage=SimpleNamespace(input_tokens=4000, output_tokens=200),
            stop_reason="end_turn",
            parsed_output=ask.Ranking(matches=[ask.Pick(id=i, why=w) for i, w in self.picks]))


def number_of(prompt: str, title: str) -> str:
    """The candidate number the prompt gave the paper with this title."""
    line = next(line for line in prompt.splitlines() if title in line)
    return line[1:line.index("]")]


def test_auto_mode_leaves_lookups_alone(lib):
    api = FakeAPI([])
    outcome = ask.search(lib.cfg, lib.conn, ["berry"], {}, mode="auto", api=api)
    assert not outcome.asked and api.prompts == []


def test_forced_ask_ranks_explains_caches_and_counts_cost(lib):
    api = FakeAPI([])
    words = ["paper", "about", "automobile", "demand"]
    ask.search(lib.cfg, lib.conn, words, {}, mode="force", api=api)       # learn the numbering
    blp = number_of(api.prompts[0], "Automobile Prices")
    lib.conn.execute("DELETE FROM asks")

    api = FakeAPI([(blp, "random-coefficients demand for cars"), ("99", "not a candidate")])
    outcome = ask.search(lib.cfg, lib.conn, words, {}, mode="force", api=api)
    assert outcome.asked and not outcome.cached
    assert titles(outcome.results) == ["Automobile Prices in Market Equilibrium"]
    assert outcome.results[0]["why"] == "random-coefficients demand for cars"
    assert outcome.cost == pytest.approx((4000 * 0.10 + 200 * 0.50) / 1e6)

    again = ask.search(lib.cfg, lib.conn, words, {}, mode="force", api=api)
    assert again.cached and again.cost == 0 and len(api.prompts) == 1   # no second call
    assert ask.spent_this_month(lib.conn) == pytest.approx(outcome.cost)


def test_confidential_files_never_reach_the_model(lib):
    api = FakeAPI([])
    ask.search(lib.cfg, lib.conn, ["gelato", "automobile", "pricing"], {}, mode="force", api=api)
    assert api.prompts and "Secret Submission" not in api.prompts[0]


def test_monthly_cap_stops_asking(lib):
    capped = dataclasses.replace(lib.cfg, ask_monthly_cap=0.0)
    api = FakeAPI([])
    outcome = ask.search(capped, lib.conn, ["automobile", "demand", "paper"], {}, mode="force", api=api)
    assert not outcome.asked and api.prompts == [] and "cap" in outcome.message
    assert outcome.results                                    # the local results are still shown


def test_ask_never_turns_off_automatic_asking(lib):
    never = dataclasses.replace(lib.cfg, ask="never")
    api = FakeAPI([])
    outcome = ask.search(never, lib.conn, ["zebra", "quantum", "harbor", "paper"], {}, mode="auto", api=api)
    assert not outcome.asked and api.prompts == []


def test_leading_question_mark_forces():
    assert ask.strip_force(["?IO", "paper"]) == (["IO", "paper"], True)
    assert ask.strip_force(["?", "IO"]) == (["IO"], True)
    assert ask.strip_force(["IO"]) == (["IO"], False)


def test_unsure_signal():
    assert ask.unsure({"keyword_hits": 0, "top_similarity": 0.65, "margin": 0.05})
    assert ask.unsure({"keyword_hits": 0, "top_similarity": 0.80, "margin": 0.01})
    assert not ask.unsure({"keyword_hits": 0, "top_similarity": 0.80, "margin": 0.05})
    assert not ask.unsure({"keyword_hits": 2, "top_similarity": 0.50, "margin": 0.0})


def test_answers_are_stored_as_json(lib):
    api = FakeAPI([])
    ask.search(lib.cfg, lib.conn, ["automobile", "demand", "paper"], {}, mode="force", api=api)
    row = lib.conn.execute("SELECT answer FROM asks").fetchone()
    assert json.loads(row["answer"]) == []
