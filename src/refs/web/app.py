"""The web interface: a small local server and a JSON API.

Run with `refs serve`. It listens on this computer only (127.0.0.1). The page
itself is three static files in web/static/; everything it shows comes from
the endpoints below, which reuse the same index and work files as the CLI.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from fastapi import Body, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .. import bib, config, index, records, resolve, venues, works
from .. import scan as scanner
from ..cli import _split_query

STATIC = Path(__file__).parent / "static"


def create_app() -> FastAPI:
    cfg = config.load()
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    # --- helpers ---------------------------------------------------------------

    def guard(marker: str | None) -> None:
        """Refuse state-changing requests that do not come from our own page.

        A browser only lets another website send this custom header after asking
        permission, which this server never grants. That stops a random web
        page from making the app open files or add papers.
        """
        if marker != "1":
            raise HTTPException(403, "missing X-Refs header")

    def shape(item: dict, all_works: dict) -> dict:
        """One search result as the page needs it."""
        work = all_works.get(item["work_id"] or "")
        meta = (work or {}).get("meta") or {}
        return {
            # Venue column: the journal for articles, otherwise what the item is.
            "venue_label": venues.label(meta) if meta else item["kind"],
            "abbr": venues.abbreviation(meta.get("venue")) if meta else None,
            "id": item["work_id"] or item["hash"],
            "work_id": item["work_id"], "citekey": item["citekey"],
            "title": item["title"], "authors": item["authors"], "year": item["year"],
            "venue": item["venue"], "kind": item["kind"], "status": item["status"],
            "tags": item["tags"], "description": item["description"],
            "locations": item["locations"],
            "read_status": (work or {}).get("user", {}).get("read_status"),
        }

    def stub_item(work: dict) -> dict:
        """A work saved without a PDF, shaped like a search result."""
        meta = work["meta"]
        return {
            "venue_label": venues.label(meta), "abbr": venues.abbreviation(meta.get("venue")),
            "id": work["id"], "work_id": work["id"], "citekey": work["citekey"],
            "title": meta.get("title"), "year": meta.get("year"), "venue": meta.get("venue"),
            "authors": [a["family"] for a in meta.get("authors") or [] if a.get("family")],
            "kind": meta.get("kind"), "status": "no-file", "tags": work.get("tags") or [],
            "description": work.get("description"), "locations": [],
            "read_status": work["user"].get("read_status"),
        }

    # --- pages -------------------------------------------------------------------

    @app.get("/")
    def home() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    # --- reading the library --------------------------------------------------------

    @app.get("/api/search")
    def search(q: str = "", view: str = "all", scope: str = "library") -> dict:
        """`q` uses the same syntax as `refs find`. `view` is a sidebar preset;
        `scope=all` also lists course material, not just library works."""
        conn = index.connect(cfg)
        all_works = works.load_all(cfg)
        if view == "wanted":
            items = [stub_item(w) for w in all_works.values() if not w["files"]]
            return {"items": items}
        terms, filters = _split_query(q.split())
        if view == "review":
            filters["status"] = "needs-review"
        try:
            found = index.search(conn, terms, filters, limit=None)
        except ValueError as error:
            return {"items": [], "error": str(error)}
        items = [shape(item, all_works) for item in found
                 if scope == "all" or item["work_id"]]
        if view == "unread":
            items = [i for i in items if i["read_status"] == "unread"]
        return {"items": items[:800]}

    @app.get("/api/sidebar")
    def sidebar() -> dict:
        """Counts for the left pane: presets, then works per course and project."""
        conn = index.connect(cfg)
        all_works = works.load_all(cfg)
        library = [i for i in index.search(conn, [], {}, limit=None) if i["work_id"]]
        contexts: dict[str, set] = {}
        fields: dict[str, int] = {}
        for item in library:
            for loc in item["locations"]:
                contexts.setdefault(loc["context"], set()).add(item["work_id"])
            for tag in item["tags"][:2]:           # the first tags are the broad fields
                fields[tag] = fields.get(tag, 0) + 1
        user = lambda i: (all_works.get(i["work_id"]) or {}).get("user", {})   # noqa: E731
        return {
            "presets": {
                "all": len(library),
                "unread": sum(user(i).get("read_status") == "unread" for i in library),
                "review": sum(i["status"] == "needs-review" for i in library),
                "wanted": sum(not w["files"] for w in all_works.values()),
            },
            "contexts": [{"key": key, "count": len(ids)} for key, ids in sorted(contexts.items())],
            "fields": sorted(fields.items(), key=lambda kv: -kv[1])[:12],
        }

    @app.get("/api/work/{work_id}")
    def work(work_id: str) -> dict:
        found = works.load_all(cfg).get(work_id)
        if found is None or "meta" not in found:
            raise HTTPException(404)
        return {"work": found, "bibtex": bib.entry(found)}

    @app.get("/file")
    def file(path: str) -> FileResponse:
        """Serve one PDF, but only a path the index knows (no arbitrary files)."""
        conn = index.connect(cfg)
        if conn.execute("SELECT 1 FROM paths WHERE path=?", (path,)).fetchone() is None:
            raise HTTPException(404)
        return FileResponse(cfg.absolute(path), media_type="application/pdf",
                            content_disposition_type="inline")

    # --- changing things ---------------------------------------------------------------

    @app.post("/api/work/{work_id}/read")
    def set_read(work_id: str, status: str = Body(embed=True),
                 x_refs: str | None = Header(default=None)) -> dict:
        guard(x_refs)
        found = works.load_all(cfg).get(work_id)
        if found is None or status not in {"unread", "reading", "read"}:
            raise HTTPException(400)
        found["user"]["read_status"] = status
        records.write_json(works.works_dir(cfg) / f"{work_id}.json", found)
        return {"read_status": status}

    def save_work(found: dict) -> dict:
        """Write a work, re-derive its metadata, and refresh the index."""
        records.write_json(works.works_dir(cfg) / f"{found['id']}.json", found)
        scanner.scan(cfg, index.connect(cfg))       # runs works.sync and rebuilds search
        return {"work": works.load_all(cfg)[found["id"]]}

    @app.post("/api/work/{work_id}/edit")
    def edit(work_id: str, fields: dict = Body(embed=True),
             x_refs: str | None = Header(default=None)) -> dict:
        """Save your corrections. They override the pipeline and are never overwritten.
        An empty `fields` removes all corrections (back to the pipeline's record)."""
        guard(x_refs)
        found = works.load_all(cfg).get(work_id)
        if found is None:
            raise HTTPException(404)
        clean = {k: v for k, v in fields.items() if k in works.EDITABLE and v not in ("", None)}
        if "year" in clean:
            clean["year"] = int(clean["year"])
        found["user"]["overrides"] = clean
        found["user"]["override_source"] = "you"
        found["user"]["confirmed"] = bool(clean)
        return save_work(found)

    @app.post("/api/work/{work_id}/confirm")
    def confirm(work_id: str, x_refs: str | None = Header(default=None)) -> dict:
        """Accept the record as it is: it leaves the review queue."""
        guard(x_refs)
        found = works.load_all(cfg).get(work_id)
        if found is None:
            raise HTTPException(404)
        found["user"]["confirmed"] = True
        return save_work(found)

    @app.post("/api/work/{work_id}/doi")
    def set_doi(work_id: str, doi: str = Body(embed=True),
                x_refs: str | None = Header(default=None)) -> dict:
        """Replace the record with the registry's record for a DOI you supply."""
        guard(x_refs)
        found = works.load_all(cfg).get(work_id)
        doi = doi.strip().lower().removeprefix("https://doi.org/")
        record = resolve.by_doi(doi) if found and doi else None
        if record is None:
            raise HTTPException(404, "no registry record for that DOI")
        fields = {k: v for k, v in record.to_dict().items() if k in works.EDITABLE and v}
        fields["venue"] = venues.clean(fields.get("venue"))
        found["user"]["overrides"] = {**fields, "kind": found["meta"].get("kind")}
        found["user"]["override_source"] = f"{record.source}, DOI given by you"
        found["user"]["confirmed"] = True
        return save_work(found)

    @app.post("/api/open")
    def open_file(path: str = Body(embed=True), reveal: bool = Body(default=False, embed=True),
                  x_refs: str | None = Header(default=None)) -> dict:
        """Open a PDF in the default viewer, or show it in Finder."""
        guard(x_refs)
        conn = index.connect(cfg)
        if conn.execute("SELECT 1 FROM paths WHERE path=?", (path,)).fetchone() is None:
            raise HTTPException(404)
        subprocess.run(["open", *(["-R"] if reveal else []), str(cfg.absolute(path))])
        return {"ok": True}

    @app.post("/api/scan")
    def rescan(x_refs: str | None = Header(default=None)) -> dict:
        guard(x_refs)
        report = scanner.scan(cfg, index.connect(cfg))
        return {"seen": report.seen, "changes": report.changes}

    @app.post("/api/add")
    def add(thing: str = Body(embed=True), x_refs: str | None = Header(default=None)) -> dict:
        """Quick add. Runs the same code as `refs add`, as a separate process,
        because it can take a while (download, model, registry)."""
        guard(x_refs)
        done = subprocess.run([sys.executable, "-m", "refs.cli", "add", thing],
                              capture_output=True, text=True, timeout=300)
        return {"ok": done.returncode == 0, "message": (done.stdout or done.stderr).strip()}

    return app
