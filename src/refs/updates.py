"""Published-version checker: find working papers and drafts that have since been published.

Economics papers routinely enter a library as NBER working papers, arXiv preprints,
SSRN postings or job market papers, and appear in a journal 1-3 years later. This
module re-queries Crossref and OpenAlex (bypassing stale cached misses), verifies
that a candidate is genuinely a published version of the same work, and surfaces
proposals under Review -> published. Accepting upgrades the work's metadata while
preserving its citekey, collections, notes, reading history and original WP info.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from . import records, resolve, venues, works
from .config import Config


def _updates_path(cfg: Config) -> Path:
    return cfg.absolute(cfg.state_dir) / "updates.json"


def _is_candidate(work: dict) -> bool:
    meta = work.get("meta") or {}
    if not meta or meta.get("kind") != "paper":
        return False
    if not meta.get("title"):
        return False
    if meta.get("is_working_paper"):
        return True
    if meta.get("status") == "extracted":
        return True
    venue = meta.get("venue") or ""
    doi = (meta.get("doi") or "").lower()
    if not venue:
        return True
    if doi.startswith(resolve._PREPRINT_DOI_PREFIXES):
        return True
    if resolve._PREPRINT_VENUE_RE.search(venue):
        return True
    return False


def candidates(cfg: Config) -> list[dict]:
    """Every working paper or draft in the library worth checking for publication."""
    all_works = works.load_all_readonly(cfg)
    found = [w for w in all_works.values() if _is_candidate(w)]
    found.sort(key=lambda w: (-(w.get("meta", {}).get("year") or 0), w.get("citekey") or ""))
    return found


def _update_key(proposed: dict) -> str:
    if doi := (proposed.get("doi") or "").strip().lower():
        return f"doi:{doi}"
    return f"title:{(proposed.get('title') or '').lower()}|{(proposed.get('venue') or '').lower()}"


def check_one(work: dict, *, refresh: bool = True) -> dict | None:
    """Check one working paper against Crossref and OpenAlex. Returns a proposal or None."""
    meta = work.get("meta") or {}
    title = meta.get("title") or ""
    surnames = [a["family"] for a in meta.get("authors") or [] if a.get("family")]
    year = meta.get("year")
    if not title:
        return None
    match, via = resolve.find_published(title, surnames, year, doi=meta.get("doi"), refresh=refresh)
    if match is None:
        return None
    rec = match.record
    proposed = {
        "title": rec.title,
        "authors": rec.authors,
        "year": rec.year,
        "venue": venues.clean(rec.venue),
        "type": rec.type or "journal-article",
        "doi": rec.doi,
        "volume": rec.volume,
        "issue": rec.issue,
        "pages": rec.pages,
        "publisher": rec.publisher,
        "source": rec.source,
    }
    if not proposed["venue"]:
        return None
    # Skip if this exact published record is already on the work or was ignored.
    if proposed["doi"] and proposed["doi"] == (meta.get("doi") or "").lower() and not meta.get("is_working_paper"):
        return None
    key = _update_key(proposed)
    if key in (work.get("user", {}).get("ignored_updates") or []):
        return None
    return {
        "work_id": work["id"],
        "citekey": work.get("citekey"),
        "title": title,
        "authors": surnames,
        "current": {
            "year": meta.get("year"),
            "venue": venues.label(meta),
            "doi": meta.get("doi"),
            "status": meta.get("status"),
        },
        "proposed": proposed,
        "key": key,
        "via": via,
        "similarity": round(match.title_similarity, 1),
        "open_url": f"https://doi.org/{proposed['doi']}" if proposed.get("doi") else None,
    }


def check_all(cfg: Config, *, refresh: bool = True,
              progress: Callable[[int, int, str], None] | None = None) -> dict:
    """Check every working paper in the library and persist proposals to `<state>/updates.json`."""
    todo = candidates(cfg)
    found: list[dict] = []
    errors = 0
    for idx, work in enumerate(todo, 1):
        if progress:
            progress(idx, len(todo), work.get("citekey") or work["id"])
        try:
            proposal = check_one(work, refresh=refresh)
        except Exception:  # noqa: BLE001 (network blip on one paper must not abort the run)
            errors += 1
            continue
        if proposal is not None:
            found.append(proposal)
    data = {
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "checked": len(todo),
        "errors": errors,
        "items": found,
    }
    records.write_json(_updates_path(cfg), data)
    return data


def load(cfg: Config) -> dict:
    """Load the saved proposals, dropping any work that was removed, ignored or already updated."""
    data = records.read_json(_updates_path(cfg)) or {"checked_at": None, "checked": 0, "items": []}
    all_works = works.load_all_readonly(cfg)
    live: list[dict] = []
    for item in data.get("items") or []:
        work = all_works.get(item.get("work_id") or "")
        if work is None or not _is_candidate(work):
            continue
        if item.get("key") in (work.get("user", {}).get("ignored_updates") or []):
            continue
        live.append(item)
    return {
        "checked_at": data.get("checked_at"),
        "checked": data.get("checked", 0),
        "candidates": sum(1 for w in all_works.values() if _is_candidate(w)),
        "items": live,
    }


def _drop_cached(cfg: Config, work_id: str) -> None:
    data = records.read_json(_updates_path(cfg))
    if not data:
        return
    items = [i for i in (data.get("items") or []) if i.get("work_id") != work_id]
    if len(items) != len(data.get("items") or []):
        data["items"] = items
        records.write_json(_updates_path(cfg), data)


def apply(cfg: Config, work_id: str, proposed: dict | None = None) -> dict:
    """Apply a published-version upgrade to `work_id`, preserving citekey, notes and collections."""
    work = works.load_one(cfg, work_id)
    if work is None:
        raise ValueError(f"no work {work_id}")
    if proposed is None:
        saved = load(cfg)
        match = next((i for i in saved["items"] if i["work_id"] == work_id), None)
        if match is None:
            raise ValueError(f"no pending published update for {work_id}")
        proposed = match["proposed"]

    # Remember the original working-paper details if we have not already saved them.
    if "working_paper" not in work and work.get("meta"):
        work["working_paper"] = {
            "title": work["meta"].get("title"),
            "year": work["meta"].get("year"),
            "venue": work["meta"].get("venue"),
            "doi": work["meta"].get("doi"),
        }

    fields = {k: v for k, v in proposed.items() if k in works.EDITABLE and v not in ("", None)}
    if fields.get("venue"):
        fields["venue"] = venues.clean(fields["venue"])
    fields["kind"] = work.get("meta", {}).get("kind") or "paper"
    fields.setdefault("type", "journal-article")
    work["user"]["overrides"] = fields
    work["user"]["override_source"] = f"{proposed.get('source') or 'registry'}, published update"
    work["user"]["confirmed"] = True
    works.apply_user_edits(work)
    works.sync_keys(work)
    records.write_json(works.works_dir(cfg) / f"{work_id}.json", work)
    _drop_cached(cfg, work_id)
    return work


def ignore(cfg: Config, work_id: str, key: str) -> None:
    """Dismiss a published-version suggestion for `work_id` so it does not come back."""
    work = works.load_one(cfg, work_id)
    if work is None:
        raise ValueError(f"no work {work_id}")
    ignored = list(work["user"].get("ignored_updates") or [])
    if key and key not in ignored:
        ignored.append(key)
        work["user"]["ignored_updates"] = ignored
        records.write_json(works.works_dir(cfg) / f"{work_id}.json", work)
    _drop_cached(cfg, work_id)
