"""Import a Zotero library: references, collections, tags, notes, citation keys.

Zotero is only read, never modified: the database is copied to a temporary
folder first (including its write-ahead log, where recent changes live).

Each Zotero reference becomes a work, or joins the work that already exists
for the same paper. PDFs are matched to files already in the index, by path
for linked attachments and by content for attachments stored inside Zotero.
"""

from __future__ import annotations

import hashlib
import re
import shutil
import sqlite3
import tempfile
import unicodedata
from pathlib import Path

from .config import Config

# Zotero item type -> the registry vocabulary used elsewhere, and our "kind".
_TYPES = {
    "journalArticle": ("journal-article", "paper"), "preprint": ("posted-content", "paper"),
    "manuscript": ("posted-content", "paper"), "report": ("report", "paper"),
    "thesis": ("report", "paper"), "conferencePaper": ("proceedings-article", "paper"),
    "bookSection": ("book-chapter", "chapter"), "book": ("book", "book"),
}


def snapshot(zotero_dir: Path) -> sqlite3.Connection:
    """Open a private copy of zotero.sqlite, so Zotero itself is never touched."""
    folder = Path(tempfile.mkdtemp(prefix="refs-zotero-"))
    for name in ("zotero.sqlite", "zotero.sqlite-wal", "zotero.sqlite-shm"):
        if (zotero_dir / name).exists():
            shutil.copy2(zotero_dir / name, folder / name)
    conn = sqlite3.connect(folder / "zotero.sqlite")
    conn.row_factory = sqlite3.Row
    return conn


def _strip_html(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text)).strip()


def read_items(conn: sqlite3.Connection) -> list[dict]:
    """Every reference in the library (trash excluded), with its children gathered."""
    live = "NOT IN (SELECT itemID FROM deletedItems)"
    items: dict[int, dict] = {}
    for row in conn.execute(
        f"SELECT i.itemID, i.key, t.typeName, i.dateAdded FROM items i "
        f"JOIN itemTypes t USING(itemTypeID) WHERE i.itemID {live} "
        f"AND t.typeName NOT IN ('attachment', 'note', 'annotation')"
    ):
        if row["typeName"] in _TYPES:
            items[row["itemID"]] = {
                "key": row["key"], "zotero_type": row["typeName"], "added": row["dateAdded"],
                "fields": {}, "authors": [], "editors": [], "collections": [], "tags": [],
                "notes": [], "attachments": [],
            }

    for row in conn.execute(
        "SELECT d.itemID, f.fieldName, v.value FROM itemData d "
        "JOIN fields f USING(fieldID) JOIN itemDataValues v USING(valueID)"):
        if row["itemID"] in items:
            items[row["itemID"]]["fields"][row["fieldName"]] = row["value"]

    for row in conn.execute(
        "SELECT ic.itemID, c.firstName, c.lastName, ct.creatorType FROM itemCreators ic "
        "JOIN creators c USING(creatorID) JOIN creatorTypes ct USING(creatorTypeID) "
        "ORDER BY ic.itemID, ic.orderIndex"):
        if row["itemID"] in items and row["lastName"]:
            person = {"given": row["firstName"] or "", "family": row["lastName"]}
            role = "authors" if row["creatorType"] == "author" else "editors"
            items[row["itemID"]][role].append(person)

    for row in conn.execute("SELECT ci.itemID, c.collectionName FROM collectionItems ci "
                            "JOIN collections c USING(collectionID)"):
        if row["itemID"] in items:
            items[row["itemID"]]["collections"].append(row["collectionName"])

    # type 0 = tags you added by hand; type 1 = keywords imported automatically.
    for row in conn.execute("SELECT it.itemID, t.name FROM itemTags it JOIN tags t USING(tagID) "
                            "WHERE it.type = 0"):
        if row["itemID"] in items:
            items[row["itemID"]]["tags"].append(row["name"])

    for row in conn.execute(f"SELECT parentItemID, note FROM itemNotes "
                            f"WHERE parentItemID IS NOT NULL AND itemID {live}"):
        if row["parentItemID"] in items and _strip_html(row["note"]):
            items[row["parentItemID"]]["notes"].append(_strip_html(row["note"]))

    for row in conn.execute(
        f"SELECT a.parentItemID, i.key, a.path, a.linkMode FROM itemAttachments a "
        f"JOIN items i USING(itemID) WHERE a.contentType = 'application/pdf' "
        f"AND a.path IS NOT NULL AND a.itemID {live}"):
        if row["parentItemID"] in items:
            items[row["parentItemID"]]["attachments"].append(
                {"key": row["key"], "path": row["path"], "linked": row["linkMode"] == 2})
    return list(items.values())


def to_record(item: dict) -> dict:
    """A Zotero item in the same shape as a registry record."""
    fields = item["fields"]
    record_type, kind = _TYPES[item["zotero_type"]]
    year = re.search(r"\b(1[5-9]\d\d|20\d\d)\b", fields.get("date", ""))
    venue = (fields.get("publicationTitle") or fields.get("bookTitle")
             or fields.get("seriesTitle") or fields.get("institution") or fields.get("repository"))
    doi = (fields.get("DOI") or "").lower().removeprefix("https://doi.org/") or None
    return {
        "title": fields.get("title") or "", "authors": item["authors"] or item["editors"],
        "year": int(year.group(1)) if year else None, "venue": venue, "type": record_type,
        "kind": kind, "doi": doi, "volume": fields.get("volume"), "issue": fields.get("issue"),
        "pages": fields.get("pages"), "publisher": fields.get("publisher"),
        "abstract": fields.get("abstractNote"), "source": "zotero",
    }


def locate_attachment(cfg: Config, zotero_dir: Path, attachment: dict,
                      known_paths: dict[str, str], known_hashes: set[str],
                      ) -> tuple[str | None, Path | None, bool]:
    """Find an attachment. Returns (content hash, file on disk, already indexed?).

    Linked files: the stored path may come from another computer, so only the
    part from a scanned folder's name onward is trusted ("…/1-Papers Library/x.pdf"
    is looked for under the root that ends in "1-Papers Library").
    Stored files: looked up by content, so a copy anywhere in Dropbox matches.
    """
    path = unicodedata.normalize("NFC", attachment["path"])
    if attachment["linked"]:
        parts = Path(path).parts
        for root in cfg.roots:
            anchor = Path(root).name
            if anchor in parts:
                rel = "/".join([root, *parts[parts.index(anchor) + 1:]])
                if rel in known_paths:
                    return known_paths[rel], cfg.absolute(rel), True
        return None, None, False
    stored = zotero_dir / "storage" / attachment["key"] / path.removeprefix("storage:")
    if not stored.is_file():
        return None, None, False
    with stored.open("rb") as handle:
        digest = hashlib.file_digest(handle, "sha256").hexdigest()
    return digest, stored, digest in known_hashes
