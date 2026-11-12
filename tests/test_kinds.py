"""Provisional document kinds, guessed from names and folders only."""

from refs.contexts import derive
from refs.index import guess_kind
from refs.naming import parse_filename


def kind(rel: str) -> str:
    return guess_kind(rel, derive(rel), parse_filename(rel.rsplit("/", 1)[-1]))


def test_papers():
    base = "phd/coursework/2026-fall/220a/readings/04-dynamic-demand"
    assert kind(f"{base}/hendel-nevo-2006-sales-and-consumer-inventory.pdf") == "paper"
    assert kind(f"{base}/A_1023536326497.pdf") == "paper"          # unnamed, but in readings
    assert kind("papers-dump/w32515.pdf") == "paper"
    assert kind("phd/research/bets/readings/lit/tax/Saez (2002, JPubE).pdf") == "paper"
    assert kind("phd/coursework/2025-fall/2-Econ 219A/Final Paper/Becker JPE 1991.pdf") == "paper"


def test_course_material():
    c = "phd/coursework/2026-fall/230a"
    assert kind(f"{c}/exams/2023-exam.pdf") == "problem-set"
    assert kind(f"{c}/exams/2023-solutions.pdf") == "problem-set"
    assert kind(f"{c}/slides/source/04-taxable-income-elasticities.pdf") == "slides"
    assert kind("phd/coursework/2026-spring/240b/part-2/Homeworks/HW1.pdf") == "problem-set"
    assert kind("phd/coursework/field_syllabi/230a_public_fall22.pdf") == "syllabus"
    assert kind("phd/coursework/2026-fall/220a/other/stanford/econ257-2026-syllabus.pdf") == "syllabus"
    # The folder decides when the file name says nothing.
    assert kind("phd/coursework/2025-summer/Lecture Notes/chapter-3.pdf") == "notes"
    assert kind("phd/coursework/2026-fall/220a/canvas-sync/unfiled/RefReportExample.pdf") == "course-material"


def test_unknown_is_left_unknown():
    assert kind("phd/coursework/2025-fall/2-Econ 219A/Final Paper/rdz026.pdf") == "unknown"
    assert kind("phd/research/bets/pdfs/writeup-2026-02-22.pdf") == "unknown"
