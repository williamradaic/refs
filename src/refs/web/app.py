"""The web interface: a small local server and a JSON API.

Run with `refs serve`. It listens on this computer only (127.0.0.1). The page
itself is three static files in web/static/; everything it shows comes from
the endpoints below, which reuse the same index and work files as the CLI.
"""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import subprocess
import sys
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import Body, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, PlainTextResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware
from fastapi.staticfiles import StaticFiles

from .. import bib, config, index, ingest, organize, records, rename, resolve, venues, works
from .. import scan as scanner
from ..cli import _split_query

STATIC = Path(__file__).parent / "static"


def create_app(watch: bool = False) -> FastAPI:
    """The server. `watch=True` also follows Dropbox for changes (watch.py)
    for as long as the server runs, which is how `refs serve` starts it."""
    cfg = config.load()
    watcher = None
    if watch:
        from ..watch import Watcher
        watcher = Watcher(cfg)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if watcher is not None:
            watcher.start()
        yield
        if watcher is not None:
            watcher.stop()

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    # --- who may talk to this server -----------------------------------------------
    # 1. Only this computer. The server already listens on 127.0.0.1 only; this
    #    check is a second lock in case it is ever started some other way.
    @app.middleware("http")
    async def local_only(request, call_next):
        client = request.client.host if request.client else ""
        if client not in {"127.0.0.1", "::1", "testclient"}:
            return PlainTextResponse("refs only answers this computer.", status_code=403)
        return await call_next(request)

    # The page's own files change with every update of refs: tell the browser
    # to check for a newer version each time (an unchanged file costs a 304).
    @app.middleware("http")
    async def revalidate(request, call_next):
        response = await call_next(request)
        if request.url.path == "/" or request.url.path.startswith("/static/") \
                or request.url.path == "/bookmarklet":
            response.headers["Cache-Control"] = "no-cache"
        return response

    # 2. Only under its own names. This stops a web page elsewhere from pointing
    #    a domain it controls at 127.0.0.1 and talking to refs through the browser.
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=[
        "localhost", "127.0.0.1", "*.localhost", "refs.test", "testserver"])

    # --- background jobs ------------------------------------------------------------
    # Adding a paper means downloading, reading and looking it up, which can
    # take from seconds to minutes. The request returns at once with a job id,
    # and the page asks how the job is doing until it finishes.
    jobs: dict[str, dict] = {}

    def start_add(thing: str, label: str) -> dict:
        job_id = secrets.token_hex(4)
        jobs[job_id] = {"state": "running", "label": label, "message": "", "started": time.time()}

        def run() -> None:
            try:
                done = subprocess.run([sys.executable, "-m", "refs.cli", "add", thing],
                                      capture_output=True, text=True, timeout=1800)
                jobs[job_id].update(state="done" if done.returncode == 0 else "failed",
                                    message=(done.stdout or done.stderr).strip())
            except Exception as error:                     # timeout, or could not start
                jobs[job_id].update(state="failed", message=f"{type(error).__name__}: {error}")

        threading.Thread(target=run, daemon=True).start()
        return {"job": job_id}

    @app.get("/api/changes")
    def changes() -> dict:
        """What the folder watcher last did. The page polls this and reloads the
        list when `generation` moves, so a PDF saved in Finder shows up by itself."""
        if watcher is None:
            return {"watching": False, "generation": 0, "last_pass": None, "message": ""}
        return watcher.status()

    @app.get("/api/job/{job_id}")
    def job(job_id: str) -> dict:
        found = jobs.get(job_id)
        if found is None:
            raise HTTPException(404)
        return {**found, "seconds": int(time.time() - found["started"])}

    # --- helpers ---------------------------------------------------------------

    def guard(marker: str | None) -> None:
        """Refuse state-changing requests that do not come from our own page.

        A browser only lets another website send this custom header after asking
        permission, which this server never grants. That stops a random web
        page from making the app open files or add papers.
        """
        if marker != "1":
            raise HTTPException(403, "missing X-Refs header")

    def dates(locations: list[dict], work: dict | None) -> tuple[str | None, str | None]:
        """(date added, date modified) as YYYY-MM-DD.

        Added: when the earliest PDF of the work appeared on disk, or the date
        it was added to Zotero if that is earlier; a work with no PDF uses the
        day it entered the library. Modified: the newest change to any of its PDFs.
        """
        created, changed = [], []
        for loc in locations:
            try:
                info = os.stat(cfg.absolute(loc["path"]))
            except OSError:
                continue
            created.append(getattr(info, "st_birthtime", info.st_mtime))
            changed.append(info.st_mtime)
        day = lambda t: datetime.fromtimestamp(t).strftime("%Y-%m-%d")   # noqa: E731
        candidates = [day(min(created))] if created else []
        user = (work or {}).get("user", {})
        if (user.get("zotero") or {}).get("added"):
            candidates.append(user["zotero"]["added"][:10])
        if not candidates and work:
            candidates.append(work["created"][:10])
        return (min(candidates) if candidates else None), (day(max(changed)) if changed else None)

    def shape(item: dict, all_works: dict) -> dict:
        """One search result as the page needs it."""
        work = all_works.get(item["work_id"] or "")
        meta = (work or {}).get("meta") or {}
        added, modified = dates(item["locations"], work)
        return {
            "added": added, "modified": modified,
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
            "added": dates([], work)[0], "modified": None,
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

    @app.get("/bookmarklet")
    def bookmarklet() -> FileResponse:
        """A page with the "+ refs" bookmarklet to drag to the bookmarks bar."""
        return FileResponse(STATIC / "bookmarklet.html")

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
        if view == "unprocessed":                  # PDFs on disk that were never read
            filters["status"], scope = "unprocessed", "all"
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
                "renames": len(rename.plan(cfg, conn, index.search(conn, [], {}, limit=None),
                                           all_works)[0]),
                "copies": len(organize.copies(cfg, conn)),
                "unprocessed": conn.execute(
                    "SELECT COUNT(*) FROM files WHERE status IS NULL "
                    "AND hash IN (SELECT hash FROM paths)").fetchone()[0],
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

    # --- review: renames, identical copies, filing -------------------------------------

    def rename_plan() -> tuple[list, list, list]:
        conn = index.connect(cfg)
        return rename.plan(cfg, conn, index.search(conn, [], {}, limit=None), works.load_all(cfg))

    def refused(error: organize.Refused) -> HTTPException:
        return HTTPException(409, str(error))

    @app.get("/api/renames")
    def renames() -> dict:
        """Every file whose name differs from the house convention, outside
        protected folders. Nothing is renamed until you approve it."""
        proposals, copies, protected = rename_plan()
        all_works = works.load_all(cfg)

        def shape_proposal(proposal) -> dict:
            meta = (all_works.get(proposal.work_id) or {}).get("meta") or {}
            return {"path": proposal.path, "new_name": proposal.new_name,
                    "folder": str(Path(proposal.path).parent), "work_id": proposal.work_id,
                    "title": meta.get("title"), "year": meta.get("year"),
                    "authors": [a["family"] for a in meta.get("authors") or [] if a.get("family")]}
        return {"proposals": [shape_proposal(p) for p in proposals],
                "protected": len(protected), "copies": copies}

    @app.post("/api/renames/apply")
    def apply_renames(paths: list[str] = Body(embed=True),
                      x_refs: str | None = Header(default=None)) -> dict:
        """Rename the chosen files. The plan is recomputed here, so the page
        can only approve names refs itself proposes."""
        guard(x_refs)
        wanted = set(paths)
        with ingest.exclusive(cfg):
            proposals = [p for p in rename_plan()[0] if p.path in wanted]
            done = rename.apply_many(cfg, proposals)
            scanner.scan(cfg, index.connect(cfg))
        return {"done": len(done)}

    @app.post("/api/undo")
    def undo(count: int = Body(embed=True), x_refs: str | None = Header(default=None)) -> dict:
        """Reverse the last `count` renames or moves (the journal is shared with the CLI)."""
        guard(x_refs)
        with ingest.exclusive(cfg):
            restored = rename.undo(cfg, max(0, min(count, 500)))
            scanner.scan(cfg, index.connect(cfg))
        return {"restored": len(restored)}

    @app.get("/api/copies")
    def copies() -> dict:
        found = organize.copies(cfg, index.connect(cfg))
        all_works = works.load_all(cfg)
        return {"items": [shape(item, all_works) | {"hash": item["hash"]} for item in found]}

    @app.post("/api/copies/keep")
    def keep(file_hash: str = Body(embed=True, alias="hash"),
             x_refs: str | None = Header(default=None)) -> dict:
        """These copies are on purpose: take them out of the Review queue."""
        guard(x_refs)
        organize.keep_copies(cfg, file_hash)
        return {"ok": True}

    @app.post("/api/trash")
    def trash(path: str = Body(embed=True), x_refs: str | None = Header(default=None)) -> dict:
        """Move one copy to the macOS Trash (never the last copy)."""
        guard(x_refs)
        with ingest.exclusive(cfg):
            conn = index.connect(cfg)
            try:
                organize.trash(cfg, conn, path)
            except organize.Refused as error:
                raise refused(error)
            scanner.scan(cfg, conn)
        return {"ok": True}

    @app.get("/api/folders")
    def folders() -> dict:
        """Where a PDF can be filed (for the "file to…" picker)."""
        return {"folders": organize.destinations(cfg, index.connect(cfg))}

    @app.post("/api/move")
    def move(path: str = Body(embed=True), folder: str = Body(embed=True),
             x_refs: str | None = Header(default=None)) -> dict:
        """File a PDF into another folder. Undo with the rename journal."""
        guard(x_refs)
        with ingest.exclusive(cfg):
            conn = index.connect(cfg)
            try:
                new_path = organize.move(cfg, conn, path, folder)
            except organize.Refused as error:
                raise refused(error)
            scanner.scan(cfg, conn)
        return {"path": new_path}

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
        return start_add(thing, thing)

    @app.post("/api/identify")
    def identify(path: str = Body(embed=True), x_refs: str | None = Header(default=None)) -> dict:
        """Run the pipeline on a PDF that is indexed but was never identified."""
        guard(x_refs)
        conn = index.connect(cfg)
        if conn.execute("SELECT 1 FROM paths WHERE path=?", (path,)).fetchone() is None:
            raise HTTPException(404)
        return start_add(str(cfg.absolute(path)), Path(path).name)

    @app.post("/api/upload")
    async def upload(request: Request, name: str = "upload.pdf",
                     x_refs: str | None = Header(default=None)) -> dict:
        """A PDF dropped on the page: save it into the dump, then add it like any other."""
        guard(x_refs)
        body = await request.body()
        if not body.startswith(b"%PDF"):
            raise HTTPException(400, "that file is not a PDF")
        # The same PDF may already be in the library: do not store a second copy.
        known = index.connect(cfg).execute(
            "SELECT p.path, f.status FROM paths p JOIN files f USING(hash) WHERE p.hash=?",
            (hashlib.sha256(body).hexdigest(),)).fetchone()
        if known is not None and known["status"] is None:
            # On disk, but never identified (an earlier attempt was interrupted).
            return start_add(str(cfg.absolute(known["path"])), Path(known["path"]).name)
        if known is not None:
            return {"ok": True, "message": f"Already in the library: {known['path']}"}
        # Keep the dropped file's name, minus anything unsafe in a file name.
        safe = re.sub(r"[^\w .()+,&'-]", "_", Path(name).name).strip() or "upload.pdf"
        if not safe.lower().endswith(".pdf"):
            safe += ".pdf"
        target = cfg.absolute(cfg.dump) / safe
        number = 2
        while target.exists():                     # never overwrite an existing file
            target = target.with_name(f"{Path(safe).stem}-{number}.pdf")
            number += 1
        target.write_bytes(body)
        return start_add(str(target), target.name)

    return app
