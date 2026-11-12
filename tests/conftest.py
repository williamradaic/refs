"""Settings shared by all tests."""

import pytest

from refs import config


@pytest.fixture(autouse=True)
def no_real_bib_files(monkeypatch):
    # The default `bib_files` point at real project folders under ~/phd. Tests
    # must not depend on (or read) those: they list their own .bib files.
    monkeypatch.setitem(config.DEFAULTS, "bib_files", [])
