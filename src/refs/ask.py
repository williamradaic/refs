"""Asking Haiku: when a search reads like a description, let a model rank it.

Local search (index.py: keywords plus meaning) answers almost every query on
its own, instantly and for free. A few queries are hard for it: "IO paper on
ice cream" mixes a field and a topic, and the closest summaries may score
only middling. For those, refs builds a shortlist of about 40 candidates
locally and asks Haiku which of them fit, best first, with a few words on why.
One call costs about $0.0005.

When it asks (mode "auto"), both of these must hold:
  1. the query reads like a description, not a lookup (query.sentence_like);
  2. the local result looks unsure: no item contains every word, and either
     the best semantic match is weak (below UNSURE_BELOW) or the top five
     are about equally good (within FLAT_MARGIN), so there is no clear winner.
A query that starts with "?" always asks (mode "force"); `ask = "never"` in
config.toml turns automatic asking off.

What reaches the API: the query and, per candidate, the title, authors, year,
venue, tags and the summary from the reading step. Never your notes, and
never files from the no-LLM (confidential) folders.

Answers are kept in the `asks` table of the index, keyed by the query and
the shortlist, so asking the same thing again is free. The table also keeps
the cost of each call: once this month's total reaches `ask_monthly_cap`,
refs stops asking and stays local until the month turns.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

import anthropic
from pydantic import BaseModel

from . import index, keys, llm, pipeline, query
from .config import Config

MODEL = llm.MODEL           # claude-haiku-5-5
EFFORT = "low"              # ranking 40 short entries needs little thinking
SHORTLIST = 40              # candidates shown to the model
MAX_MATCHES = 10
UNSURE_BELOW = 0.72         # best cosine similarity below this: a weak match
FLAT_MARGIN = 0.02          # best minus fifth best below this: no clear winner
ESTIMATED_COST = 0.001      # a generous guess at one call, for the monthly cap

SCHEMA = """
CREATE TABLE IF NOT EXISTS asks (
    key     TEXT PRIMARY KEY,       -- fingerprint of model, query and shortlist
    created TEXT NOT NULL,          -- UTC, ISO format
    query   TEXT NOT NULL,
    answer  TEXT NOT NULL,          -- JSON list of [item id, why]
    cost    REAL NOT NULL           -- dollars, at list price
);
"""

SYSTEM = """\
You help a PhD student in economics find a paper in their own library. You get \
their description of what they are looking for and a numbered list of \
candidates from the library. Return the candidates that match the description, \
best match first, at most ten. Include a candidate only if it plausibly is what \
they mean; an empty list is the right answer when none fits. Read field \
abbreviations as economists use them (IO = industrial organization, PF = public \
finance, RDD = regression discontinuity, and so on). For each match, `why` says \
in at most twelve words what about it fits the description.\
"""


class Pick(BaseModel):
    id: str          # the candidate's number, as in "[7]"
    why: str


class Ranking(BaseModel):
    matches: list[Pick]


@dataclass
class Outcome:
    """A search, and whether (and how) Haiku took part in it."""

    results: list[dict]
    asked: bool = False             # Haiku ranked these results
    cached: bool = False            # ... in an earlier, identical search
    cost: float = 0.0               # dollars this search spent
    candidates: int = 0             # size of the shortlist Haiku saw
    message: str = ""               # one line for the person, if there is news
    report: dict = field(default_factory=dict)   # index.search's signals


# --- Deciding ---------------------------------------------------------------------

def strip_force(words: list[str]) -> tuple[list[str], bool]:
    """A leading "?" (alone or on the first word) means: ask, whatever the signals say."""
    if words and words[0].startswith("?"):
        first = words[0][1:]
        return ([first] if first else []) + words[1:], True
    return words, False


def unsure(report: dict) -> bool:
    """The second signal: is the local ranking without a convincing answer?"""
    if report.get("keyword_hits"):
        return False                 # some item contains every word of the query
    top = report.get("top_similarity")
    if top is None:
        return True                  # no vectors yet, or nothing to compare
    return top < UNSURE_BELOW or (report.get("margin") or 0.0) < FLAT_MARGIN


def spent_this_month(conn: sqlite3.Connection) -> float:
    month_start = datetime.now(timezone.utc).strftime("%Y-%m-01")
    try:
        row = conn.execute("SELECT COALESCE(SUM(cost), 0) AS total FROM asks WHERE created >= ?",
                           (month_start,)).fetchone()
    except sqlite3.OperationalError:
        return 0.0
    return float(row["total"])


# --- Building the request -----------------------------------------------------------

def _summary(text: str | None, limit: int = 300) -> str:
    """The first sentence or two of a summary, at most about `limit` characters."""
    if not text:
        return ""
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= limit:
        return text
    cut = text.rfind(". ", 0, limit)
    return text[:cut + 1] if cut > 80 else text[:limit].rsplit(" ", 1)[0] + " …"


def _line(number: int, item: dict) -> str:
    authors = item["authors"] or []
    who = ", ".join(authors[:3]) + (" et al." if len(authors) > 3 else "")
    fields = [f"[{number}] {who} ({item['year'] or 'n.d.'}). {item['title'] or 'untitled'}."]
    if item["venue"]:
        fields.append(f"{item['venue']}.")
    fields.append(f"Kind: {item['kind'] or 'unknown'}.")
    if item["tags"]:
        fields.append(f"Tags: {', '.join(item['tags'][:6])}.")
    if summary := _summary(item["description"]):
        fields.append(f"Summary: {summary}")
    return " ".join(fields)


def _confidential(cfg: Config, item: dict) -> bool:
    return item["status"] == "confidential" or any(
        pipeline._is_confidential(cfg, loc["path"]) for loc in item["locations"])


def _key(text: str, filters: dict, shortlist: list[dict]) -> str:
    ids = [i["work_id"] or i["hash"] for i in shortlist]
    blob = json.dumps([MODEL, " ".join(text.lower().split()), sorted(filters.items()), ids])
    return hashlib.sha256(blob.encode()).hexdigest()[:24]


# --- Searching ----------------------------------------------------------------------

def search(cfg: Config, conn: sqlite3.Connection, terms: list[str], filters: dict[str, str],
           *, mode: str = "auto", keep: Callable[[dict], bool] = lambda item: True,
           api: anthropic.Anthropic | None = None) -> Outcome:
    """Local search, then Haiku if `mode` and the signals say so.

    mode "off" never asks, "auto" asks when both signals hold (and config
    allows), "force" always asks. `keep` drops items the caller will not show
    (the web page's library-only view), so Haiku never ranks them. `api`
    replaces the real client (tests).
    """
    report: dict = {}
    local = [i for i in index.search(conn, terms, filters, limit=None, semantic=True, report=report)
             if keep(i)]
    outcome = Outcome(results=local, report=report)
    if not terms or mode == "off":
        return outcome
    if mode == "auto":
        if cfg.ask == "never":
            return outcome
        if not (query.sentence_like(terms, index.surnames(conn)) and unsure(report)):
            return outcome

    # The shortlist: a wider local search (any word may match, no similarity
    # cut-off), minus what the caller hides and what must never reach an LLM.
    wide = [i for i in index.search(conn, terms, filters, limit=None, semantic=True, wide=True)
            if keep(i) and not _confidential(cfg, i)][:SHORTLIST]
    if not wide:
        outcome.message = "nothing in the library to ask about"
        return outcome
    text = " ".join(terms)
    key = _key(text, filters, wide)
    by_id = {i["work_id"] or i["hash"]: i for i in wide}

    try:
        saved = conn.execute("SELECT answer FROM asks WHERE key=?", (key,)).fetchone()
    except sqlite3.OperationalError:
        conn.execute(SCHEMA.strip())
        saved = None
    if saved is not None:
        picks = json.loads(saved["answer"])
        outcome.cached = True
    else:
        if spent_this_month(conn) + ESTIMATED_COST > cfg.ask_monthly_cap:
            outcome.message = (f"not asking Haiku: this month's cap of ${cfg.ask_monthly_cap:.2f} "
                               "is reached (ask_monthly_cap in config.toml)")
            return outcome
        if api is None:
            if not keys.get_api_key():
                outcome.message = "not asking Haiku: no API key (run `refs setup-key`)"
                return outcome
            api = llm.client()
        prompt = (f"<description>{text}</description>\n\n<candidates>\n"
                  + "\n".join(_line(n, item) for n, item in enumerate(wide, 1))
                  + "\n</candidates>")
        try:
            response = api.messages.parse(
                model=MODEL, max_tokens=8000, system=SYSTEM,
                messages=[{"role": "user", "content": prompt}],
                output_format=Ranking, output_config={"effort": EFFORT},
            )
        except anthropic.APIError as error:
            outcome.message = f"Haiku unavailable ({type(error).__name__}); showing local results"
            return outcome
        usage = {"input_tokens": response.usage.input_tokens,
                 "output_tokens": response.usage.output_tokens}
        outcome.cost = llm.cost(usage)
        if response.stop_reason in {"refusal", "max_tokens"} or response.parsed_output is None:
            outcome.message = f"Haiku gave no usable answer ({response.stop_reason}); showing local results"
            _record(conn, key, text, [], outcome.cost, save=False)
            return outcome
        # Numbers back to items; ignore anything that is not one of ours.
        picks = []
        for pick in response.parsed_output.matches[:MAX_MATCHES]:
            number = pick.id.strip().strip("[]")
            if number.isdigit() and 1 <= int(number) <= len(wide):
                item = wide[int(number) - 1]
                picks.append([item["work_id"] or item["hash"], pick.why.strip()])
        _record(conn, key, text, picks, outcome.cost)

    outcome.asked, outcome.candidates = True, len(wide)
    results, seen = [], set()
    for item_id, why in picks:
        if (item := by_id.get(item_id)) is not None and item_id not in seen:
            seen.add(item_id)
            results.append({**item, "why": why})
    outcome.results = results
    spent = "earlier answer, free" if outcome.cached else f"${outcome.cost:.4f}"
    outcome.message = (f"ranked by Haiku from {len(wide)} candidates ({spent})" if results else
                       f"Haiku found nothing that fits among {len(wide)} candidates ({spent})")
    return outcome


def _record(conn: sqlite3.Connection, key: str, text: str, picks: list, cost: float,
            save: bool = True) -> None:
    """Keep the answer (and always its cost, which counts toward the monthly cap).
    A failed call is stored under a key no search will look up again."""
    created = datetime.now(timezone.utc).isoformat(timespec="seconds")
    stored_key = key if save else f"failed:{key}:{created}"
    conn.execute(SCHEMA.strip())
    conn.execute("INSERT OR REPLACE INTO asks VALUES (?,?,?,?,?)",
                 (stored_key, created, text, json.dumps(picks, ensure_ascii=False), cost))
    conn.commit()
