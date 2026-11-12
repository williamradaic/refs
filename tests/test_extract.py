"""Identifier detection on first-page text, using snippets from real papers."""

from refs.extract import clean_doi, find_ids, fingerprint


def test_doi_on_an_aer_first_page():
    text = ("American Economic Review 2025, 115(5): 1400–1438 "
            "https://doi.org/10.1257/aer.20220276 1400 Does Entry Remedy Collusion?")
    assert find_ids(text) == {"doi": "10.1257/aer.20220276"}


def test_doi_cleanup():
    assert clean_doi("10.1257/aer.20220276.") == "10.1257/aer.20220276"
    assert clean_doi("10.1016/S0304-405X(98)00027-0)") == "10.1016/s0304-405x(98)00027-0"
    assert clean_doi("10.1093/QJE/qjz026,") == "10.1093/qje/qjz026"


def test_nber_needs_nber_context():
    nber = "NBER WORKING PAPER SERIES ... Working Paper 32515 http://www.nber.org/papers/w32515"
    assert find_ids(nber)["nber"] == "32515"
    # "Working Paper 123" from some other institution is not an NBER number.
    assert "nber" not in find_ids("Centre for Something, Working Paper No. 1234, 2020")


def test_other_identifiers():
    assert find_ids("arXiv:2301.01234v2 [econ.TH] 3 Jan 2023")["arxiv"] == "2301.01234"
    assert find_ids("available at https://ssrn.com/abstract=3036660")["ssrn"] == "3036660"
    assert find_ids("Stable URL: https://www.jstor.org/stable/2171802 Accessed")["jstor"] == "2171802"
    assert find_ids("BY STEVEN BERRY, JAMES LEVINSOHN, AND ARIEL PAKES") == {}


def test_fingerprint_ignores_layout_but_not_content():
    text = "Automobile Prices in Market Equilibrium. " * 20
    assert fingerprint(text) == fingerprint(text.upper().replace(" ", "\n"))
    assert fingerprint(text) != fingerprint("A different paper entirely. " * 20)
    assert fingerprint("too short") is None


def test_ligatures_do_not_corrupt_a_doi():
    # Typeset PDFs often store "fi" as the single ligature character U+FB01.
    from refs.extract import clean_text
    raw = "Journal of Financial Economics\nhttps://doi.org/10.1016/j.j\ufb01neco.2023.103745  Received"
    assert find_ids(clean_text(raw)) == {"doi": "10.1016/j.jfineco.2023.103745"}
