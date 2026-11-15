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
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware
from fastapi.staticfiles import StaticFiles

from .. import (bib, config, index, ingest, keys, notes, onboarding, organize, reading, records, rename, resolve,
                settings, venues, works)
from .. import ask as asking
from .. import contexts as contexts_mod     # sidebar() has a local named `contexts`
from .. import scan as scanner
from ..cli import _split_query

STATIC = Path(__file__).parent / "static"


def _refs_origin(origin: str) -> bool:
    """Is this Origin header one of refs' own addresses (any port)?"""
    from urllib.parse import urlsplit
    host = (urlsplit(origin).hostname or "").lower()
    return host in {"localhost", "127.0.0.1", "::1", "refs.test"} or host.endswith(".localhost")


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

    # A file written by a newer refs is never overwritten (records.py): say so.
    @app.exception_handler(records.NewerData)
    async def newer_data(_request, error: records.NewerData):
        return JSONResponse({"error": str(error), "detail": str(error)}, status_code=409)
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

    # 3. Only from refs' own page. A browser names the site a request comes from
    #    (Origin) on every POST; anything that changes something must come from a
    #    refs address. Requests without an Origin (the command line, scripts) are
    #    still held to the X-Refs header that guard() checks.
    @app.middleware("http")
    async def same_site_only(request, call_next):
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            if origin is not None and not _refs_origin(origin):
                return PlainTextResponse("refs only takes changes from its own page.", status_code=403)
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

    def start_add(thing: str, label: str, queue: bool = False) -> dict:
        job_id = secrets.token_hex(4)
        jobs[job_id] = {"state": "running", "label": label, "message": "", "started": time.time()}

        def run() -> None:
            try:
                done = subprocess.run([sys.executable, "-m", "refs.cli", "add", thing,
                                       *(["--queue"] if queue else [])],
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
            **read_fields(work),
        }

    def read_fields(work: dict | None) -> dict:
        """Read status (yours, else detected from your markup) and queue time."""
        if not work:
            return {"read_status": None, "read_source": None, "queued": None}
        status, source = reading.effective(work)
        return {"read_status": status, "read_source": source, "queued": work["user"].get("queued")}

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
            **read_fields(work),
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
    def search(q: str = "", view: str = "all", scope: str = "library", ask: str = "off") -> dict:
        """`q` uses the same syntax as `refs find`. `view` is a sidebar preset;
        `scope=all` also lists course material, not just library works.
        `ask`: "off" while typing (local only); "auto" when Enter is pressed,
        which asks Haiku if the query reads like a description and the local
        results look unsure, or if it starts with "?" (ask.py)."""
        conn = index.connect(cfg)
        all_works = works.load_all(cfg)
        if view == "wanted":
            items = [stub_item(w) for w in all_works.values() if not w["files"]]
            return {"items": items}
        user = lambda i: (all_works.get(i["work_id"] or "") or {}).get("user", {})  # noqa: E731
        words, forced = asking.strip_force(q.split())
        terms, filters = _split_query(words)
        if view == "review":
            filters["status"] = "needs-review"
        if view == "unprocessed":                  # PDFs on disk that were never read
            filters["status"], scope = "unprocessed", "all"
        mode = "off" if ask == "off" else ("force" if forced else "auto")
        # Haiku ranks only what the view shows.
        if view in reading.RANK:                   # unread, reading, read
            keep = lambda i: bool(i["work_id"]) and i["work_id"] in all_works and (  # noqa: E731
                reading.effective(all_works[i["work_id"]])[0] == view)
        elif view == "queue":
            keep = lambda i: bool(user(i).get("queued"))  # noqa: E731
        else:
            keep = lambda i: scope == "all" or bool(i["work_id"])  # noqa: E731
        try:
            outcome = asking.search(cfg, conn, terms, filters, mode=mode, keep=keep)
        except ValueError as error:
            return {"items": [], "error": str(error)}
        items = [shape(item, all_works) | {"why": item.get("why")} for item in outcome.results]
        if view == "queue" and not terms:          # the queue's own order: first queued first
            items.sort(key=lambda i: i["queued"] or "")
        return {"items": items[:800], "asked": outcome.asked, "message": outcome.message}

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
        # Each context's area and term, so the page groups without parsing keys.
        about = {row["context"]: {"area": row["area"], "term": row["term"]} for row in conn.execute(
            "SELECT context, MIN(area) AS area, MIN(term) AS term FROM paths GROUP BY context")}
        user = lambda i: (all_works.get(i["work_id"]) or {}).get("user", {})   # noqa: E731
        return {
            "presets": {
                "all": len(library),
                # Works by read status (yours, else detected): unread, reading, read.
                **{status: sum(i["work_id"] in all_works
                               and reading.effective(all_works[i["work_id"]])[0] == status
                               for i in library) for status in reading.RANK},
                # Queued works, with or without a PDF.
                "queue": sum(bool(w["user"].get("queued")) for w in all_works.values()
                             if "meta" in w),
                "review": sum(i["status"] == "needs-review" for i in library),
                "wanted": sum(not w["files"] for w in all_works.values()),
                "renames": len(rename.plan(cfg, conn, index.search(conn, [], {}, limit=None),
                                           all_works)[0]),
                "copies": len(organize.copies(cfg, conn)),
                "unprocessed": conn.execute(
                    "SELECT COUNT(*) FROM files WHERE status IS NULL "
                    "AND hash IN (SELECT hash FROM paths)").fetchone()[0],
            },
            "contexts": [{"key": key, "count": len(ids), **about.get(key, {})}
                         for key, ids in sorted(contexts.items())],
            # Areas the sidebar lists by name, in order (others go under "Other").
            "areas": contexts_mod.layout_for(cfg).areas,
            "fields": sorted(fields.items(), key=lambda kv: -kv[1])[:12],
            # The quick-add folder's name, for the page's hints ("save it into …").
            "dump": cfg.dump,
        }

    @app.get("/api/work/{work_id}")
    def work(work_id: str) -> dict:
        found = works.load_all(cfg).get(work_id)
        if found is None or "meta" not in found:
            raise HTTPException(404)
        return {"work": found, "bibtex": bib.entry(found), "note": notes.read(cfg, found)}

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
        if found is None or status not in reading.RANK:
            raise HTTPException(400)
        reading.set_status(found, status)          # reading it also takes it off the queue
        return save_reading(found)

    @app.post("/api/work/{work_id}/queue")
    def set_queue(work_id: str, on: bool = Body(embed=True),
                  x_refs: str | None = Header(default=None)) -> dict:
        """Put a work on the reading queue, or take it off."""
        guard(x_refs)
        found = works.load_all(cfg).get(work_id)
        if found is None:
            raise HTTPException(404)
        reading.set_queued(found, on)
        return save_reading(found)

    def save_reading(found: dict) -> dict:
        """Write a work's read status or queue change, and copy it into the
        index so `read:` and `queued:` searches see it (no rescan needed)."""
        records.write_json(works.works_dir(cfg) / f"{found['id']}.json", found)
        conn = index.connect(cfg)
        conn.execute("UPDATE files SET read_status=?, queued=? WHERE work_id=?",
                     (reading.effective(found)[0], found["user"].get("queued"), found["id"]))
        conn.commit()
        return read_fields(found)

    @app.post("/api/work/{work_id}/note")
    def save_note(work_id: str, body: str = Body(embed=True), version: str = Body(embed=True),
                  x_refs: str | None = Header(default=None)) -> dict:
        """Save your reading note (a Markdown file in the Obsidian vault).
        409 if the file changed since the page loaded it (edited in Obsidian)."""
        guard(x_refs)
        found = works.load_all(cfg).get(work_id)
        if found is None:
            raise HTTPException(404)
        try:
            return {"note": notes.write(cfg, found, body, version)}
        except notes.Conflict:
            raise HTTPException(409, "the note changed in Obsidian since this page loaded it")
        except FileNotFoundError as error:
            raise HTTPException(404, str(error))

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
        conn, all_works = index.connect(cfg), works.load_all(cfg)
        def rows(found):
            return [shape(item, all_works) | {"hash": item["hash"]} for item in found]
        return {"items": rows(organize.copies(cfg, conn)),
                "kept": rows(organize.copies(cfg, conn, kept=True))}

    @app.post("/api/copies/keep")
    def keep(file_hash: str = Body(embed=True, alias="hash"),
             x_refs: str | None = Header(default=None)) -> dict:
        """These copies are on purpose: take them out of the Review queue."""
        guard(x_refs)
        organize.keep_copies(cfg, file_hash)
        return {"ok": True}

    @app.post("/api/copies/unkeep")
    def unkeep(file_hash: str = Body(embed=True, alias="hash"),
               x_refs: str | None = Header(default=None)) -> dict:
        """Undo "keep all": the copies come back to the Review queue."""
        guard(x_refs)
        organize.unkeep_copies(cfg, file_hash)
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

    @app.post("/api/copy")
    def copy(path: str = Body(embed=True), folder: str = Body(embed=True),
             x_refs: str | None = Header(default=None)) -> dict:
        """Copy a PDF into another folder; the original stays. Undo trashes the copy."""
        guard(x_refs)
        with ingest.exclusive(cfg):
            conn = index.connect(cfg)
            try:
                new_path = organize.copy(cfg, conn, path, folder)
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

    # --- settings (settings.py) ---------------------------------------------------------

    def apply_settings(new_cfg: config.Config) -> None:
        """Use new settings from now on, without a restart. Every endpoint reads
        `cfg` when it runs, so replacing it here is enough for them. The watcher
        is told too; its next pass rescans (and reads new PDFs as auto_process
        says). Without a watcher (--no-watch) a plain scan runs, which reads
        nothing new."""
        nonlocal cfg
        cfg = new_cfg
        if watcher is not None:
            watcher.reconfigure(new_cfg)
        else:
            threading.Thread(target=lambda: scanner.scan(new_cfg, index.connect(new_cfg)),
                             daemon=True).start()

    @app.get("/api/settings")
    def get_settings():
        """The settings page: config.toml as refs reads it now, field by field."""
        try:
            return settings.current()
        except SystemExit as error:                   # the file has a mistake in it
            return JSONResponse({"error": str(error)}, status_code=400)

    @app.post("/api/settings")
    def save_settings(changes: dict = Body(embed=True), approve_reads: bool = Body(default=False, embed=True),
                      x_refs: str | None = Header(default=None)):
        """Check and save changed settings, then use them at once. `approve_reads`:
        the first run's "read everything", whose cost the page showed, counts as
        your OK for the batch it starts (budget.py)."""
        guard(x_refs)
        try:
            new_cfg = settings.save(changes)
        except settings.SettingError as error:
            return JSONResponse({"error": str(error), "key": error.key}, status_code=400)
        apply_settings(new_cfg)
        if approve_reads and watcher is not None:
            watcher.approve()
        return settings.current(new_cfg)

    @app.post("/api/read/approve")
    def approve_reads(x_refs: str | None = Header(default=None)) -> dict:
        """Yes to the batch of unread PDFs the page showed: read them now."""
        guard(x_refs)
        if watcher is not None:
            watcher.approve()
        return {"ok": watcher is not None}

    @app.post("/api/settings/folder")
    def choose_folder(prompt: str = Body(embed=True), kind: str = Body(default="folders", embed=True),
                      x_refs: str | None = Header(default=None)) -> dict:
        """Open the macOS folder dialog (it appears on this Mac, in front)."""
        guard(x_refs)
        start = cfg.obsidian_vault.parent if kind == "vault" and cfg.obsidian_vault \
            else Path.home() if kind in {"vault", "library"} else cfg.dropbox_root
        chosen = settings.choose_folder(prompt, start)
        if chosen is None:
            return {"path": None}
        # Folders inside the library are shown and stored relative to it.
        if kind in {"folders", "folder"}:
            try:
                return {"path": str(chosen.resolve().relative_to(cfg.dropbox_root))}
            except ValueError:
                return {"path": None, "error": f"{chosen} is not inside the library folder"}
        return {"path": str(chosen)}

    # --- first run (onboarding.py); the answers are saved through /api/settings ------------

    @app.get("/api/onboarding")
    def onboarding_state() -> dict:
        """Whether refs still needs setting up, and what the first-run page offers."""
        from .. import service
        # The library folder refs was given (config.toml or REFS_DROPBOX_ROOT)
        # comes first; your home folder only stands in when none was found.
        libraries = onboarding.library_candidates()
        if cfg.dropbox_root != Path.home().resolve() and \
                all(lib["path"] != str(cfg.dropbox_root) for lib in libraries):
            libraries.insert(0, {"label": "set now", "path": str(cfg.dropbox_root)})
        return {
            "needed": config.not_set_up(cfg) is not None,
            "libraries": libraries,
            "library": str(cfg.dropbox_root),
            "dump": cfg.dump,
            "vaults": settings.obsidian_vaults(),
            "api_key": settings.api_key_status(),
            "watching": watcher is not None,
            "service": "running" if service.is_loaded() else "installed" if service.PLIST.exists() else "none",
            "cost_per_pdf": onboarding.COST_PER_PDF,
            "read_cap": cfg.read_monthly_cap,
        }

    @app.get("/api/onboarding/survey")
    def onboarding_survey(root: str) -> dict:
        """Folders of a would-be library folder, with PDF counts (only looks)."""
        folder = Path(root).expanduser()
        if not folder.is_dir():
            return JSONResponse({"error": f"there is no folder {root}"}, status_code=400)
        return onboarding.survey(folder.resolve())

    @app.get("/api/settings/authors")
    def found_authors() -> dict:
        """Annotation author names in the library's PDFs (a few seconds)."""
        return {"authors": settings.annotation_authors(cfg, index.connect(cfg))}

    @app.post("/api/settings/key")
    def save_key(key: str = Body(embed=True), x_refs: str | None = Header(default=None)):
        """Store the Anthropic API key in the Keychain. It is never sent back."""
        guard(x_refs)
        key = key.strip()
        if problem := keys.problem_with(key):         # checked with Anthropic first
            return JSONResponse({"error": problem}, status_code=400)
        keys.set_api_key(key)
        return {"api_key": settings.api_key_status()}

    @app.post("/api/scan")
    def rescan(x_refs: str | None = Header(default=None)) -> dict:
        guard(x_refs)
        report = scanner.scan(cfg, index.connect(cfg))
        return {"seen": report.seen, "changes": report.changes}

    @app.post("/api/add")
    def add(thing: str = Body(embed=True), queue: bool = Body(default=False, embed=True),
            x_refs: str | None = Header(default=None)) -> dict:
        """Quick add. Runs the same code as `refs add`, as a separate process,
        because it can take a while (download, model, registry). `queue` also
        puts the paper on the reading queue."""
        guard(x_refs)
        return start_add(thing, thing, queue)

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
