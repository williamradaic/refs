"""What reading PDFs costs, and the two brakes on automatic reading.

Every reading by the model is entered in a small ledger in the index (dollars
at list price, from the tokens used). Two rules then apply to what the folder
watcher reads on its own (watch.py):

* The monthly limit (`read_monthly_cap` in config.toml): once this calendar
  month's reading reaches it, automatic reading stops until the month turns.
  New PDFs are still indexed; they wait under "not identified".
* Large batches wait for your OK: more than BULK unread PDFs at once (outside
  the quick-add folder) are not read until you approve them on the page, which
  shows how many and roughly what they cost. Adding a folder with hundreds of
  PDFs never quietly costs dollars.

Papers you add yourself (`refs add`, the add bar) are always read, and
`refs process` asks before a large run.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from .config import Config

# More unread PDFs than this in one pass wait for approval.
BULK = 25
# A guess at one reading, for estimates (llm.py: about $0.001 with Haiku).
PER_PDF = 0.001

SCHEMA = """
CREATE TABLE IF NOT EXISTS spending (
    created TEXT NOT NULL,          -- UTC, ISO format
    kind    TEXT NOT NULL,          -- "read" (asking has its own table, ask.py)
    cost    REAL NOT NULL           -- dollars, at list price
);
"""


def record(conn: sqlite3.Connection, cost: float, kind: str = "read") -> None:
    """Enter one reading's cost in the ledger."""
    if cost <= 0:
        return
    conn.executescript(SCHEMA)
    conn.execute("INSERT INTO spending VALUES (?, ?, ?)",
                 (datetime.now(timezone.utc).isoformat(timespec="seconds"), kind, cost))
    conn.commit()


def spent_this_month(conn: sqlite3.Connection, kind: str = "read") -> float:
    conn.executescript(SCHEMA)
    month_start = datetime.now(timezone.utc).strftime("%Y-%m-01")
    row = conn.execute("SELECT COALESCE(SUM(cost), 0) FROM spending WHERE kind = ? AND created >= ?",
                       (kind, month_start)).fetchone()
    return float(row[0])


def left_this_month(cfg: Config, conn: sqlite3.Connection) -> float:
    """Dollars automatic reading may still spend this month."""
    return max(0.0, cfg.read_monthly_cap - spent_this_month(conn))


def estimate(pdfs: int) -> float:
    return round(pdfs * PER_PDF, 2)
