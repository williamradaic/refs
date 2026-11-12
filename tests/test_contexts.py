"""Folder rules, checked against real paths from the Dropbox tree."""

from refs.contexts import course_slug, derive, pretty_topic


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
