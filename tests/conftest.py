"""Settings shared by all tests."""

import hashlib
import re

import numpy as np
import pytest

from refs import config, embed

# Words the fake embedding model treats as meaning the same thing, so tests can
# check that semantic search finds a paper with no word in common with the query.
SYNONYMS = {
    "frozen": "icecream", "dessert": "icecream", "desserts": "icecream",
    "ice": "icecream", "cream": "icecream",
    "cars": "automobile", "car": "automobile", "automobiles": "automobile",
    "tax": "taxation", "taxes": "taxation", "taxation": "taxation",
}


def fake_encoder(texts: list[str], query: bool) -> np.ndarray:
    """A stand-in for the real model: a bag of words (synonyms merged), hashed
    into 64 dimensions and scaled to length 1. Cheap, deterministic, offline."""
    rows = np.zeros((len(texts), 64), dtype=np.float32)
    for i, text in enumerate(texts):
        for word in re.findall(r"[a-z]+", text.lower()):
            word = SYNONYMS.get(word, word)
            rows[i, int(hashlib.md5(word.encode()).hexdigest(), 16) % 64] += 1.0
    norms = np.linalg.norm(rows, axis=1, keepdims=True)
    return rows / np.maximum(norms, 1e-12)


# The tests' library is laid out like a PhD student's Dropbox (phd/coursework/
# <term>/<course>, phd/research/<project>, a papers-dump), with private folders
# to keep out. These settings describe it; the shipped defaults are generic.
TEST_LIBRARY = {
    "roots": ["phd", "papers-dump"],
    "dump": "papers-dump",
    "state_dir": "phd/tools/refs",
    "exclude_paths": ["phd/admission", "phd/move-in"],
    "ignore_dirs": [*config.DEFAULTS["ignore_dirs"], "admin"],
    "no_llm": ["phd/referee"],
    "no_rename": [*config.DEFAULTS["no_rename"], "canvas-sync"],
    "bib_files": [],                   # tests list their own .bib files
}


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "generic_defaults: run with the shipped defaults, not the test library's")


@pytest.fixture(autouse=True)
def test_library_settings(monkeypatch, tmp_path, request):
    # Never the real ~/.config/refs/config.toml (it names real folders, a real
    # vault and real .bib files). A test that needs a file sets its own.
    monkeypatch.setenv("REFS_CONFIG", str(tmp_path / "no-config.toml"))
    if request.node.get_closest_marker("generic_defaults"):
        return
    for key, value in TEST_LIBRARY.items():
        monkeypatch.setitem(config.DEFAULTS, key, value)
    # A throwaway Obsidian vault, never the real one.
    monkeypatch.setitem(config.DEFAULTS, "obsidian_vault", str(tmp_path / "test-vault"))


@pytest.fixture(autouse=True)
def fake_model():
    # Never load (and download) the real embedding model: the fake one above.
    embed.set_encoder(fake_encoder)
    yield
    embed.set_encoder(None)
