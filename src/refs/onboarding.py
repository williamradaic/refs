"""First run: what the onboarding (web page and `refs init`) needs to ask.

Onboarding runs while refs is not set up (no watched folders, config.not_set_up)
and asks, in order: the library folder, the folders that hold papers (shown as
a tree with PDF counts), the quick-add folder, private folders, the Obsidian
vault, and the API key and how much to read. The answers are saved with
settings.save(), like any change on the settings page.

Everything here only looks: it lists folders and counts PDFs, nothing more.
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import config, contexts

# Cost of reading one PDF (llm.py, Haiku; about $0.001), for the estimate.
COST_PER_PDF = 0.001
# How deep the tree the page shows goes (counts still include everything below).
TREE_DEPTH = 3
# A large library: stop counting after this many seconds and say so.
TIME_LIMIT = 20.0

# Folder names that suggest paperwork (leave out entirely) or confidential
# work (never send to the AI). Whole words only, so "taxation" is not "tax".
# The "likely" ones are ticked for you; the others are only offered.
_PAPERWORK_LIKELY = re.compile(r"\b(visa|passports?|immigration|i-?20|ds-?2019)\b", re.I)
_PAPERWORK = re.compile(r"\b(taxes|tax-returns?|insurance|medical|banking|payroll|paystubs?|"
                        r"lease|receipts?|reimbursements?|contracts?|admin|personal|private)\b", re.I)
_CONFIDENTIAL = re.compile(r"\b(referee|refereeing|referee-reports?|confidential)\b", re.I)


def _private_looking(rel: str, name: str) -> bool:
    """A folder named like paperwork or confidential work. Only short names
    count ("Taxes", "visa", "referee"): a long one is a paper or a topic ("O'DR
    2006 Sin Taxes", "tax-design-insurance"), and nothing inside a readings
    folder is paperwork."""
    if "reading" in rel.lower():
        return False
    words = [w for w in re.split(r"[\s_\-]+", re.sub(r"^\d+[\s_\-.]+", "", name)) if w]
    if len(words) > 2:
        return False
    return bool(_PAPERWORK_LIKELY.search(name) or _PAPERWORK.search(name) or _CONFIDENTIAL.search(name))


@dataclass
class Folder:
    """One folder of the tree: its PDFs include every folder below it."""

    name: str
    path: str                                   # relative to the library folder
    pdfs: int = 0
    children: list[Folder] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"name": self.name, "path": self.path, "pdfs": self.pdfs,
                "children": [c.as_dict() for c in sorted(self.children, key=lambda c: c.name.lower())]}

    def term_parent(self) -> bool:
        """Its folders are terms ("Fall 2026", "2025-spring"): courses live
        inside them, so a rule "<this>/{term}/{course}" fits."""
        named = [c for c in self.children if c.pdfs]
        terms = [c for c in named if contexts.term_of(c.name)]
        return bool(terms) and 2 * len(terms) >= len(named)


def library_candidates() -> list[dict]:
    """Folders on this Mac that could hold a library, most likely first."""
    home = Path.home()
    cloud = home / "Library" / "CloudStorage"
    found: list[tuple[str, Path]] = []
    if (dropbox := config.find_dropbox()) is not None:
        found.append(("Dropbox", dropbox))
    found.append(("iCloud Drive", home / "Library" / "Mobile Documents" / "com~apple~CloudDocs"))
    if cloud.is_dir():
        for entry in sorted(cloud.iterdir()):
            if entry.name.startswith("GoogleDrive-"):
                found.append(("Google Drive", entry / "My Drive"))
            elif entry.name.startswith("OneDrive"):
                found.append(("OneDrive", entry))
            elif entry.name.startswith("Box"):
                found.append(("Box", entry))
    found.append(("Documents", home / "Documents"))
    seen: set[Path] = set()
    out = []
    for label, path in found:
        if path.is_dir() and path.resolve() not in seen:
            seen.add(path.resolve())
            out.append({"label": label, "path": str(path.resolve())})
    return out


def survey(root: Path, ignore_dirs: frozenset[str] | None = None,
           time_limit: float = TIME_LIMIT) -> dict:
    """Walk the library folder once: the tree of folders with PDF counts (to
    TREE_DEPTH levels), folders whose names suggest they are private, and
    whether the walk finished in time. Hidden folders, app bundles and the
    usual ignored names (.git, node_modules, ...) are skipped."""
    ignore = ignore_dirs if ignore_dirs is not None else frozenset(
        n.lower() for n in config.DEFAULTS["ignore_dirs"])
    top = Folder(name=root.name, path="")
    nodes: dict[str, Folder] = {"": top}
    suggestions: list[dict] = []
    started = time.monotonic()
    complete = True

    for current, dirs, files in os.walk(root):
        if time.monotonic() - started > time_limit:
            complete = False
            break
        rel = os.path.relpath(current, root)
        rel = "" if rel == "." else rel
        depth = 0 if not rel else rel.count(os.sep) + 1
        # Prune in place: os.walk then never enters these.
        dirs[:] = [d for d in dirs if not d.startswith(".") and d.lower() not in ignore
                   and not d.endswith((".app", ".photoslibrary", ".bundle"))]
        for d in dirs:
            child_rel = f"{rel}/{d}" if rel else d
            if depth < TREE_DEPTH:
                node = Folder(name=d, path=child_rel)
                nodes[rel].children.append(node)
                nodes[child_rel] = node
            if _private_looking(child_rel, d):
                if _CONFIDENTIAL.search(d):
                    suggestions.append({"path": child_rel, "kind": "no_llm", "ticked": True})
                else:
                    suggestions.append({"path": child_rel, "kind": "exclude",
                                        "ticked": bool(_PAPERWORK_LIKELY.search(d))})
        pdfs = sum(1 for f in files if f.lower().endswith(".pdf") and not f.startswith("._"))
        if pdfs:
            # Add to this folder's count and to every shown folder above it.
            parts = rel.split(os.sep) if rel else []
            for i in range(min(len(parts), TREE_DEPTH) + 1):
                nodes["/".join(parts[:i])].pdfs += pdfs

    # A suggested folder inside another suggested one adds nothing, unless it is
    # ticked and its parent is not ("Personal" offered, "Personal/visa" ticked).
    kept = [s for s in suggestions
            if not any(s["path"].startswith(o["path"] + "/") and (o["ticked"] or not s["ticked"])
                       for o in suggestions)]
    rules = [f"{node.path}/{{term}}/{{course}}" for node in nodes.values()
             if node.path and node.term_parent()]
    return {"tree": top.as_dict(), "suggestions": kept, "term_rules": rules, "complete": complete,
            "seconds": round(time.monotonic() - started, 1)}


def estimate(pdfs: int) -> dict:
    """What reading this many PDFs costs, roughly."""
    dollars = pdfs * COST_PER_PDF
    return {"pdfs": pdfs, "dollars": round(dollars, 2),
            "minutes": max(1, round(pdfs * 4 / 60))}       # about 4 s a PDF, one at a time


def _under(path: str, folders) -> bool:
    return any(path == f or path.startswith(f + "/") for f in folders)


def pdfs_in(tree: dict, roots: list[str], exclude: list[str] = ()) -> int:
    """PDFs in the chosen folders, from a survey's tree (as the page counts them)."""
    if _under(tree["path"], roots) and tree["path"] and not _under(tree["path"], exclude):
        return tree["pdfs"]
    return sum(pdfs_in(child, roots, exclude) for child in tree["children"])


def rules_for(found: dict, roots: list[str]) -> list[str]:
    """The survey's term rules that lie inside the chosen folders."""
    return [r for r in found["term_rules"] if _under(r.split("/{term}")[0], roots)]
