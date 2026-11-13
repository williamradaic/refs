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


@pytest.fixture(autouse=True)
def no_real_bib_files(monkeypatch):
    # The default `bib_files` point at real project folders under ~/phd. Tests
    # must not depend on (or read) those: they list their own .bib files.
    monkeypatch.setitem(config.DEFAULTS, "bib_files", [])


@pytest.fixture(autouse=True)
def isolated_vault_and_model(monkeypatch, tmp_path):
    # Nor may they read the real Obsidian vault, or load (and download) the
    # real embedding model: a throwaway vault and the fake model above.
    monkeypatch.setitem(config.DEFAULTS, "obsidian_vault", str(tmp_path / "test-vault"))
    embed.set_encoder(fake_encoder)
    yield
    embed.set_encoder(None)
