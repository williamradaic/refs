"""Folder rules, checked against real paths from the Dropbox tree."""

import pytest

from refs.contexts import Layout, RuleError, Rule, course_slug, pretty_topic, term_of

# The "phd" preset on a library watched at phd/ and papers-dump/.
derive = Layout([], ["phd", "papers-dump"], "papers-dump", "phd").derive


def test_course_reading_with_topic():
    ctx = derive(
        "phd/coursework/2026-fall/220a/readings/03-demand-estimation-ii/"
        "berry-levinsohn-pakes-1995-automobile-prices-in-market-equilibrium.pdf"
    )
    assert ctx.key == "coursework/2026-fall/220a"
    assert (ctx.area, ctx.term, ctx.slug) == ("coursework", "2026-fall", "220a")
    assert ctx.section == "readings"
    assert (ctx.topic, ctx.topic_order) == ("Demand estimation II", 3)


def test_old_style_course_folder_names():
    assert course_slug("1-Econ 201A") == "201a"
    assert course_slug("99-Econ C110") == "c110"
    assert course_slug("220a") == "220a"
    ctx = derive("phd/coursework/2025-fall/3-Econ 240A/Bayes_Bootstrap_Nov2025.pdf")
    assert ctx.key == "coursework/2025-fall/240a"
    assert ctx.section is None


def test_single_course_term_keeps_material_folders_as_sections():
    # 2025-summer holds one course directly: "Section Notes" is not a course.
    ctx = derive("phd/coursework/2025-summer/Section Notes/204+Section+6-1.pdf")
    assert ctx.key == "coursework/2025-summer"
    assert ctx.slug is None
    assert ctx.section == "Section Notes"


def test_canvas_sync_is_a_section():
    ctx = derive("phd/coursework/2026-fall/220a/canvas-sync/slides/Week 4 Fall 2026.pdf")
    assert ctx.key == "coursework/2026-fall/220a"
    assert ctx.section == "canvas-sync"
    assert ctx.topic is None


def test_research_project_with_unnumbered_topic():
    ctx = derive("phd/research/bets/readings/lit/some-paper.pdf")
    assert (ctx.key, ctx.area, ctx.slug) == ("research/bets", "research", "bets")
    assert (ctx.topic, ctx.topic_order) == ("Lit", None)


def test_referee_and_dump_and_misc():
    assert derive("phd/referee/journal-a/doe2026/pdfs/report.pdf").key == "referee/journal-a/doe2026"
    assert derive("phd/referee/journal-a/doe2026/pdfs/report.pdf").section == "pdfs"
    assert derive("papers-dump/w32515.pdf").key == "dump"
    assert derive("phd/coursework/field_syllabi/236a_macro_fall22.pdf").key == "coursework/field_syllabi"
    assert derive("phd/admission/Visit Day/Reimbursement/x.pdf").key == "admission"
    assert derive("phd/admission/Visit Day/Reimbursement/x.pdf").section == "Visit Day"


def test_pretty_topic():
    assert pretty_topic("10-insurance-markets-and-choice-frictions") == (
        "Insurance markets and choice frictions", 10)
    assert pretty_topic("02-demand-estimation-i") == ("Demand estimation I", 2)
    assert pretty_topic("extra") == ("Extra", None)


# --- Layouts other than the phd preset ----------------------------------------

def test_generic_layout_makes_each_folder_in_a_root_a_context():
    lay = Layout([], ["Papers", "Grad School/Classes", "inbox"], "inbox")
    ctx = lay.derive("Papers/IO/readings/02-entry/bresnahan-reiss-1991.pdf")
    assert (ctx.key, ctx.area, ctx.slug) == ("Papers/IO", "Papers", "IO")
    assert (ctx.section, ctx.topic, ctx.topic_order) == ("readings", "Entry", 2)
    assert lay.derive("Papers/loose.pdf").key == "Papers"
    # A watched folder deeper in the library: its own name is the area.
    assert lay.derive("Grad School/Classes/Econ 101/syllabus.pdf").key == "Classes/Econ 101"
    assert lay.derive("inbox/w32515.pdf").key == "dump"
    # The sidebar lists the watched folders as areas (the dump has its own place).
    assert lay.areas == ["Papers", "Classes"]


def test_own_rules_come_first_and_name_their_areas():
    lay = Layout(["Grad/Classes/{term}/{course}", "Grad/Research/{name}"], ["Grad", "inbox"], "inbox")
    ctx = lay.derive("Grad/Classes/Fall 2025/Econ 240A/notes.pdf")
    assert (ctx.key, ctx.area, ctx.term, ctx.slug) == ("Classes/2025-fall/240a", "Classes", "2025-fall", "240a")
    assert lay.derive("Grad/Research/bets/lit/x.pdf").key == "Research/bets"
    # Other folders in Grad/ still get the generic rule, tried last; but Grad
    # is reached by the rules, so it is not an area of its own ("Other").
    assert lay.derive("Grad/Misc/x.pdf").key == "Grad/Misc"
    assert lay.areas == ["Classes", "Research"]


def test_rules_match_folder_names_regardless_of_case():
    lay = Layout(["papers/{name}"], ["Papers"], "inbox")
    ctx = lay.derive("Papers/Labor/x.pdf")
    assert (ctx.key, ctx.area) == ("papers/Labor", "papers")     # the rule's spelling


def test_term_folders_in_any_common_spelling():
    for folder in ["2026-fall", "Fall 2026", "fall-2026", "2026 Autumn", "FALL_2026"]:
        assert term_of(folder) == "2026-fall"
    assert term_of("2026-falls") is None and term_of("fall") is None


def test_bad_rules_are_reported_by_name():
    with pytest.raises(RuleError, match="unknown placeholder"):
        Rule.parse("Classes/{semester}")
    with pytest.raises(RuleError, match="needs a plain folder name"):
        Rule.parse("{term}/{course}")
    assert Rule.parse("{area}").parts == ("{area}",)
