"""The reading step: a small model looks at a PDF's opening pages.

Division of labour (the reliability rule of this project):
  * The model TRANSCRIBES what is printed and classifies the document.
  * Registries (resolve.py) supply the bibliographic facts.
The model's transcription is only used to *find* the registry record, and is
kept as the fallback for documents no registry knows (drafts, job market papers).

The file name is deliberately never shown to the model, so the name remains an
independent check on the result.
"""

from __future__ import annotations

import base64
from typing import Literal, Optional

import anthropic
from pydantic import BaseModel

from . import keys

MODEL = "claude-haiku-5-5"
# How hard the model thinks. "medium" is this model's default; stated explicitly
# so a future change of default cannot silently alter results or cost.
EFFORT = "medium"
# List price in dollars per million tokens (prompts up to 100K tokens).
PRICE_INPUT, PRICE_OUTPUT = 0.10, 0.50

FIELDS = [
    "microeconomic theory", "behavioral economics", "public economics",
    "industrial organization", "econometrics", "macroeconomics", "labor economics",
    "development economics", "finance", "political economy", "health economics",
    "international trade", "economic history", "experimental economics",
    "environmental economics", "urban economics", "education", "psychology",
    "law and economics", "other",
]
METHODS = [
    "theory", "structural estimation", "reduced-form", "quasi-experiment",
    "field experiment", "lab experiment", "survey", "descriptive", "simulation",
    "econometric method", "literature review",
]


# --- What the model returns ---------------------------------------------------------

class Author(BaseModel):
    given: str
    family: str


class Reading(BaseModel):
    """One document, as read off its opening pages."""

    kind: Literal["paper", "book", "chapter", "slides", "syllabus", "notes",
                  "problem-set", "report", "other"]
    title: Optional[str]
    authors: list[Author]
    year: Optional[int]
    venue: Optional[str]
    volume: Optional[str]
    issue: Optional[str]
    pages: Optional[str]
    is_working_paper: bool
    description: Optional[str]
    fields: list[str]
    methods: list[str]
    topics: list[str]
    confidence: Literal["high", "medium", "low"]
    notes: Optional[str]


class ReadingError(Exception):
    """The model did not produce a usable reading (refusal or cut-off output)."""


SYSTEM = f"""\
You are the reading step of a personal reference manager used by an economics PhD \
student. You receive the opening pages of one PDF, either as text extracted by \
software or as an image of the first page when the PDF is a scan.

Your output is used in two ways. First, a later step searches bibliographic \
registries (Crossref, OpenAlex) for the title and authors you report, and takes the \
official metadata from there. Second, when no registry knows the document, your \
transcription is all the library has. Both uses depend on you reporting what is \
actually printed on these pages. Do not fill a field from memory of the paper: if \
something is not on the pages, return null (or an empty list). A null is useful; a \
plausible guess silently corrupts the library.

Extracted text is often messy: missing spaces ("BYSTEVENBERRY"), broken accents, \
running headers, a JSTOR or publisher cover page before the article. Repair obvious \
extraction damage when the intended text is clear, and read past cover pages to the \
article itself.

Fields:
- kind: "paper" (journal article, working paper, job market paper, preprint), \
"book" (a whole book or several chapters of one), "chapter" (one chapter of an \
edited volume or handbook), "slides" (lecture or talk slides), "syllabus" (syllabus \
or reading list), "notes" (lecture notes, handouts, section notes), "problem-set" \
(problem sets, exams, solutions), "report" (policy or industry report, referee \
report, memo), "other" (forms, letters, anything else).
- title: the document's title as printed, in normal capitalisation (convert ALL \
CAPS), including any subtitle.
- authors: in printed order, split into given and family names. Only people \
credited as authors of this document.
- year: the year printed for this version (publication year for a published \
article, the draft or series date for a working paper).
- venue: the journal, series or publisher as printed, e.g. "Econometrica" or \
"NBER Working Paper".
- volume, issue, pages: for a published article or chapter, the volume, issue \
number and page range exactly as printed on these pages (pages as a range such as \
"841-890"). Null when not printed; always null for working papers.
- is_working_paper: true for working papers, preprints, drafts and job market \
papers; false for published articles and everything else.
- description: for a paper, book or chapter, two or three plain sentences saying \
what question it asks, how it answers it, and what it finds, based only on the text \
you were given. Null for other kinds, or when the pages show too little to say.
- fields: one or two broad areas, chosen from this list: {", ".join(FIELDS)}.
- methods: zero to two, chosen from this list: {", ".join(METHODS)}.
- topics: two to five specific topics in lowercase, e.g. "demand estimation", \
"present bias". Prefer a topic from the existing list in the request when one fits, \
so the library's vocabulary stays small.
- confidence: "high" when title and authors are clearly legible, "medium" when you \
had to repair or infer something, "low" when the pages are too damaged or ambiguous \
to trust.
- notes: one short sentence on anything the user should know (for example "authors \
garbled by OCR"); otherwise null.

For kinds other than paper, book and chapter, fields, methods and topics can be \
empty lists.\
"""


def client() -> anthropic.Anthropic:
    """An API client using the key from the Keychain (or the environment)."""
    key = keys.get_api_key()
    if not key:
        raise SystemExit("No Anthropic API key found. Run `refs setup-key` first.")
    # max_retries: the SDK waits and retries on rate limits and server errors.
    return anthropic.Anthropic(api_key=key, max_retries=5)


def read(api: anthropic.Anthropic, *, text: str | None, image_png: bytes | None,
         pages: int, known_topics: list[str]) -> tuple[Reading, dict]:
    """Ask the model to read one document. Returns the reading and token usage.

    Exactly one of `text` (the normal case) and `image_png` (a scan) is given.
    """
    vocabulary = ", ".join(known_topics[:300]) or "(none yet)"
    request = (
        f"The PDF has {pages} pages in total. Existing topics in the library: "
        f"{vocabulary}.\n\nRead the document and report on it."
    )
    if text is not None:
        content = [{
            "type": "text",
            "text": f"<opening_pages>\n{text}\n</opening_pages>\n\n{request}",
        }]
    else:
        content = [
            {"type": "image", "source": {
                "type": "base64", "media_type": "image/png",
                "data": base64.standard_b64encode(image_png).decode("ascii"),
            }},
            {"type": "text", "text": f"This is the first page of a scanned PDF. {request}"},
        ]

    # messages.parse() sends the Reading schema to the API, which then guarantees
    # the reply is valid JSON of that shape, and returns it as a Reading object.
    response = api.messages.parse(
        model=MODEL,
        max_tokens=16000,          # a ceiling, not a target; room for thinking + answer
        system=SYSTEM,
        messages=[{"role": "user", "content": content}],
        output_format=Reading,
        output_config={"effort": EFFORT},
    )
    usage = {
        "input_tokens": response.usage.input_tokens,
        "output_tokens": response.usage.output_tokens,
    }
    if response.stop_reason in {"refusal", "max_tokens"} or response.parsed_output is None:
        raise ReadingError(f"model stopped with {response.stop_reason!r}")
    return response.parsed_output, usage


def cost(usage: dict) -> float:
    """Dollar cost of one call at list price."""
    return (usage.get("input_tokens", 0) * PRICE_INPUT
            + usage.get("output_tokens", 0) * PRICE_OUTPUT) / 1_000_000
