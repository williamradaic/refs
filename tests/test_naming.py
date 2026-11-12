"""File-name parsing and canonical names, using names found in the library."""

from refs.naming import canonical_name, parse_filename, slugify


def test_house_convention():
    g = parse_filename("berry-levinsohn-pakes-1995-automobile-prices-in-market-equilibrium.pdf")
    assert g.style == "convention"
    assert g.authors == ["berry", "levinsohn", "pakes"]
    assert g.year == 1995
    assert g.title == "automobile prices in market equilibrium"
    assert g.looks_like_paper


def test_convention_drops_ocr_marker_and_handles_missing_authors():
    g = parse_filename("berry-1994-estimating-discrete-choice-models-ocr.pdf")
    assert g.title == "estimating discrete choice models"
    g = parse_filename("2012-the-welfare-effects-of-bundling.pdf")
    assert g.authors == [] and g.year == 2012
    assert not g.looks_like_paper      # no authors: not enough to call it a paper


def test_identifier_names():
    assert parse_filename("w32515.pdf").ids == {"nber": "32515"}
    assert parse_filename("ssrn-3036660.pdf").ids == {"ssrn": "3036660"}
    assert parse_filename("2301.01234v2.pdf").ids == {"arxiv": "2301.01234"}


def test_jstor_and_loose_styles():
    g = parse_filename("Grossman-CostsBenefitsOwnership-1986.pdf")
    assert (g.style, g.authors, g.year) == ("jstor", ["Grossman"], 1986)
    assert g.title == "Costs Benefits Ownership"

    g = parse_filename("Allen et al 2017.pdf")
    assert (g.style, g.authors, g.year) == ("loose", ["Allen"], 2017)

    g = parse_filename("Braghieri_Levy_Makarin_AER_2022.pdf")
    assert g.authors == ["Braghieri", "Levy", "Makarin"] and g.year == 2022

    g = parse_filename("KR 2008 Happiness.pdf")
    assert g.authors == ["KR"] and g.year == 2008 and g.title == "Happiness"


def test_url_encoded_name():
    g = parse_filename("heidhues-k%C5%91szegi-2010-exploiting-na%C3%AFvete.pdf")
    assert g.authors == ["heidhues", "kőszegi"] and g.year == 2010


def test_unrecognised_names_fall_back_to_a_title():
    g = parse_filename("IO Canon Reading Guide.pdf")
    assert g.style == "none" and g.title == "IO Canon Reading Guide"
    assert not g.looks_like_paper
    assert parse_filename("A_1023536326497.pdf").style == "none"


def test_slugify_handles_accents():
    assert slugify("Kőszegi") == "koszegi"
    assert slugify("O'Donoghue") == "odonoghue"
    assert slugify("Scott Morton") == "scott-morton"


def test_canonical_name_matches_existing_files():
    assert canonical_name(
        ["Berry", "Levinsohn", "Pakes"], 1995, "Automobile Prices in Market Equilibrium"
    ) == "berry-levinsohn-pakes-1995-automobile-prices-in-market-equilibrium.pdf"
    # Long title: cut to six words, not ending on a filler word.
    assert canonical_name(
        ["Berry"], 1994, "Estimating Discrete-Choice Models of Product Differentiation", ocr=True
    ) == "berry-1994-estimating-discrete-choice-models-of-product-ocr.pdf"
    # Four or more authors collapse; subtitle after the colon is dropped.
    assert canonical_name(
        ["Ghili", "Handel", "Hendel", "Whinston"], 2024,
        "Optimal Long-Term Health Insurance Contracts: Characterization, Computation",
    ) == "ghili-et-al-2024-optimal-long-term-health-insurance-contracts.pdf"


def test_bracketed_year_style_used_in_research_folders():
    g = parse_filename("Atkinson and Stiglitz (2015, LPE).pdf")
    assert (g.authors, g.year) == (["Atkinson", "Stiglitz"], 2015)
    g = parse_filename("Dickson et al (2025, REStat).pdf")
    assert (g.authors, g.year) == (["Dickson"], 2025)
    g = parse_filename("Angeletos Werning (AER 2006).pdf")
    assert (g.authors, g.year) == (["Angeletos", "Werning"], 2006)
    g = parse_filename("Bernheim 1994 - A theory of conformity-ocr.pdf")
    assert (g.authors, g.year, g.title) == (["Bernheim"], 1994, "A theory of conformity-ocr")


def test_look_alikes_are_not_mistaken_for_the_convention():
    assert parse_filename("writeup-2026-02-22.pdf").style == "none"       # a date
    assert parse_filename("econ257-2026-syllabus.pdf").style == "none"    # a course code
    assert not parse_filename("2023-exam.pdf").looks_like_paper


def test_elsevier_download_name_yields_old_style_doi():
    assert parse_filename("1-s2.0-S0304405X98000270-main.pdf").ids == {
        "doi": "10.1016/s0304-405x(98)00027-0"}
    assert parse_filename("1-s2.0-0167268180900517-main.pdf").ids == {
        "doi": "10.1016/0167-2681(80)90051-7"}
