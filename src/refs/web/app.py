"""The web interface: a small local server and a JSON API.

Run with `refs serve`. It listens on this computer only (127.0.0.1). The page
itself is three static files in web/static/; everything it shows comes from
the endpoints below, which reuse the same index and work files as the CLI.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
from email.parser import BytesParser
from email.policy import default as email_policy
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
from contextlib import asynccontextmanager, closing
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Body, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from starlette.middleware.trustedhost import TrustedHostMiddleware
from fastapi.staticfiles import StaticFiles

from .. import (annotations, batch, bib, capture, collection, config, downloads, highlights, inbox, index,
                ingest, keys, library, notes, onboarding, organize, reading, records, rename, resolve,
                settings, shared, updates, venues, works)
from .. import ask as asking
from .. import keep as keeping              # an endpoint below is called `keep`
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
    #    refs address. The bookmarklet's token-authenticated capture endpoints
    #    (/api/capture-upload, /capture-form) verify the per-Mac secret instead so
    #    they still work when a publisher sets Cross-Origin-Opener-Policy.
    _TOKEN_CAPTURE_PATHS = {"/api/capture-upload", "/capture-form"}

    @app.middleware("http")
    async def same_site_only(request, call_next):
        if request.method not in {"GET", "HEAD", "OPTIONS"} \
                and request.url.path not in _TOKEN_CAPTURE_PATHS:
            origin = request.headers.get("origin")
            if origin is not None and not _refs_origin(origin):
                return PlainTextResponse("refs only takes changes from its own page.", status_code=403)
        return await call_next(request)

    def _cors_headers(request: Request) -> dict[str, str]:
        origin = request.headers.get("origin") or "*"
        return {
            "Access-Control-Allow-Origin": origin,
            "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type, X-Refs-Token",
            "Access-Control-Allow-Private-Network": "true",
            "Vary": "Origin",
        }

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

    def start_add(thing: str | list[str], label: str, queue: bool = False, hints: dict | None = None,
                  collections: list[str] | None = None) -> dict:
        """`hints`: what the browser read off the paper's page (doi, pdf_url,
        source_url), passed on as `refs add` options; with them, refs does
        not fetch the page again. A list of `thing`s (PDFs taken in from
        Downloads) is one job: `refs add` for each in turn, with "2 of 5" as
        the message while it runs."""
        job_id = secrets.token_hex(4)
        jobs[job_id] = {"state": "running", "label": label, "message": "", "started": time.time()}
        options = ["--queue"] if queue else []
        for key, flag in (("doi", "--doi"), ("pdf_url", "--pdf-url"), ("source_url", "--source-url")):
            if (hints or {}).get(key):
                options += [flag, str(hints[key])]
        if hints and (hints.get("doi") or hints.get("pdf_url")):
            options.append("--no-page")
        for name in collections or []:                # courses and projects to put it in
            options += ["--to", name]
        things = [thing] if isinstance(thing, str) else list(thing)

        def run() -> None:
            failed, last = [], ""
            for n, one in enumerate(things, 1):
                if len(things) > 1:
                    jobs[job_id]["message"] = f"{n} of {len(things)}: {Path(one).name}"
                try:
                    done = subprocess.run([sys.executable, "-m", "refs.cli", "add", one, *options],
                                          capture_output=True, text=True, timeout=1800)
                    last = (done.stdout or done.stderr).strip()
                    if done.returncode != 0:
                        failed.append(f"{Path(one).name}: {last.splitlines()[0] if last else 'failed'}")
                except Exception as error:                 # timeout, or could not start
                    last = f"{type(error).__name__}: {error}"
                    failed.append(f"{Path(one).name}: {last}")
            if len(things) == 1:
                jobs[job_id].update(state="failed" if failed else "done", message=last)
            else:
                summary = f"read {len(things) - len(failed)} of {len(things)} PDFs"
                jobs[job_id].update(state="failed" if len(failed) == len(things) else "done",
                                    message="\n".join([summary + (f"; {len(failed)} failed" if failed else ""),
                                                       *failed]))

        threading.Thread(target=run, daemon=True).start()
        return {"job": job_id}

    def db():
        return closing(index.connect(cfg))

    def collect_known(paths: list[str], names: list[str]) -> list[str]:
        """PDFs refs already has, added again with collections: put their
        papers in those collections. Returns the names used."""
        if not names or not paths:
            return []
        with db() as conn:
            ids = [row["work_id"] for path in paths
                   if (row := conn.execute("SELECT f.work_id FROM paths p JOIN files f ON f.hash = p.hash "
                                           "WHERE p.path=?", (path,)).fetchone()) and row["work_id"]]
            used = shared.collect(cfg, conn, ids, names) if ids else []
        if used:
            library_changed()
        return used

    @app.get("/api/changes")
    def changes() -> dict:
        """What the folder watcher last did. The page polls this and reloads the
        list when `generation` moves, so a PDF saved in Finder shows up by itself."""
        if watcher is None:
            return {"watching": False, "generation": 0, "last_pass": None, "message": "",
                    "progress": None, "pending": False, "recent": []}
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

    # Dates: each is worked out as a moment (seconds since 1970) so the list
    # can sort by time of day as well, and shown as this Mac's YYYY-MM-DD.
    def moment(stamp: str | None) -> float | None:
        """An ISO time stamp as seconds. refs, Zotero ("2023-04-05 12:34:56")
        and the index all store UTC, so a stamp without a zone is UTC."""
        if not stamp:
            return None
        try:
            when = datetime.fromisoformat(stamp)
        except ValueError:
            return None
        return (when if when.tzinfo else when.replace(tzinfo=timezone.utc)).timestamp()

    _day_cache: dict[int, str] = {}

    def local_day(t: float | None) -> str | None:
        if t is None:
            return None
        # Local timezone offsets change at most on hour/half-hour boundaries, so
        # bucketing by 1800s (30 min) is exact across DST transitions and avoids
        # repeated strftime calls across hundreds of rows.
        bucket = int(t) // 1800
        day = _day_cache.get(bucket)
        if day is None:
            day = datetime.fromtimestamp(t).strftime("%Y-%m-%d")
            if len(_day_cache) > 4096:
                _day_cache.clear()
            _day_cache[bucket] = day
        return day

    def dates(locations: list[dict], work: dict | None) -> tuple[float | None, float | None]:
        """(added, modified) as moments.

        Added: when the earliest PDF of the work appeared on disk, or when it
        was added to Zotero if that is earlier; a work with no PDF uses the
        moment it entered the library. Modified: the newest change to any of its PDFs.
        """
        created, changed = [], []
        root_str = str(cfg.dropbox_root)
        for loc in locations:
            try:
                info = os.stat(f"{root_str}/{loc['path']}")
            except OSError:
                continue
            created.append(getattr(info, "st_birthtime", info.st_mtime))
            changed.append(info.st_mtime)
        candidates = [min(created)] if created else []
        user = (work or {}).get("user", {})
        if (zotero_added := moment((user.get("zotero") or {}).get("added"))) is not None:
            candidates.append(zotero_added)
        if not candidates and work and (made := moment(work.get("created"))) is not None:
            candidates.append(made)
        return (min(candidates) if candidates else None), (max(changed) if changed else None)

    def in_refs(item: dict | None, work: dict | None) -> float | None:
        """When the paper entered refs: when its work was made (for a Zotero
        import, when it was added to Zotero), else when refs first indexed
        the PDF. Unlike "added", this ignores the file's own dates."""
        if work and work.get("created"):
            return moment(work["created"])
        return moment((item or {}).get("first_seen"))

    def date_fields(added: float | None, modified: float | None, entered: float | None) -> dict:
        """The three dates as days to show, and as moments (`*_at`) to sort by."""
        return {"added": local_day(added), "modified": local_day(modified), "in_refs": local_day(entered),
                "added_at": added, "modified_at": modified, "in_refs_at": entered}

    def shape(item: dict, all_works: dict, note_keys: set[str] = frozenset()) -> dict:
        """One search result as the page needs it."""
        work = all_works.get(item["work_id"] or "")
        meta = (work or {}).get("meta") or {}
        added, modified = dates(item["locations"], work)
        user_tags = (work or {}).get("user", {}).get("tags") or []
        citekey = item["citekey"]
        return {
            **date_fields(added, modified, in_refs(item, work)),
            # Venue column: the journal for articles, otherwise what the item is.
            "venue_label": venues.label(meta) if meta else item["kind"],
            "abbr": venues.abbreviation(meta.get("venue")) if meta else None,
            "id": item["work_id"] or item["hash"],
            "work_id": item["work_id"], "citekey": citekey,
            "title": item["title"], "authors": item["authors"], "year": item["year"],
            "venue": item["venue"], "kind": item["kind"], "status": item["status"],
            "tags": item["tags"], "user_tags": user_tags,
            "description": item["description"],
            "locations": item["locations"],
            "has_note": bool(citekey and citekey in note_keys),
            "marks": item.get("marks") or 0,
            "marked_pages": item.get("marked_pages") or 0,
            "bib_keys": (work or {}).get("bib_keys") or {},
            # Collections: all of them (yours and the folders'), and yours alone.
            "collections": item.get("collections") or [],
            "own_collections": collection.explicit(work or {}),
            **read_fields(work),
        }

    def read_fields(work: dict | None) -> dict:
        """Read status (yours, else detected from your markup), reading log and queue details."""
        if not work:
            return {"read_status": None, "read_source": None, "queued": None,
                    "queue_note": None, "queue_for": None, "queue_priority": None,
                    "read_at": None, "reads": []}
        status, source = reading.effective(work)
        user = work["user"]
        return {
            "read_status": status, "read_source": source,
            "queued": user.get("queued"),
            "queue_note": user.get("queue_note"),
            "queue_for": user.get("queue_for"),
            "queue_priority": user.get("queue_priority"),
            "read_at": reading.last_read_at(work),
            "reads": user.get("reads") or [],
        }

    def stub_item(work: dict, note_keys: set[str] = frozenset()) -> dict:
        """A work saved without a PDF, shaped like a search result."""
        meta = work["meta"]
        user_tags = work["user"].get("tags") or []
        tags = list(dict.fromkeys([*(work.get("tags") or []), *user_tags]))
        citekey = work["citekey"]
        return {
            **date_fields(dates([], work)[0], None, in_refs(None, work)),
            "venue_label": venues.label(meta), "abbr": venues.abbreviation(meta.get("venue")),
            "id": work["id"], "work_id": work["id"], "citekey": citekey,
            "title": meta.get("title"), "year": meta.get("year"), "venue": meta.get("venue"),
            "authors": [a["family"] for a in meta.get("authors") or [] if a.get("family")],
            "kind": meta.get("kind"), "status": "no-file", "tags": tags, "user_tags": user_tags,
            "description": work.get("description"), "locations": [],
            "has_note": bool(citekey and citekey in note_keys),
            "marks": 0, "marked_pages": 0,
            "bib_keys": work.get("bib_keys") or {},
            "collections": collection.explicit(work), "own_collections": collection.explicit(work),
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

    @app.get("/capture")
    def capture_page() -> FileResponse:
        """The small window the bookmarklet opens: it receives the paper from
        the page and follows the add."""
        return FileResponse(STATIC / "capture.html")

    @app.get("/api/bookmarklet")
    def bookmarklet_token() -> dict:
        """The key built into the bookmarklet (capture.token). Another website
        cannot read this answer: the browser keeps it from them (no CORS), and
        TrustedHostMiddleware stops one that points its own name at 127.0.0.1."""
        return {"token": capture.token(cfg), "proxy": cfg.library_proxy}

    # --- reading the library --------------------------------------------------------

    @app.get("/api/search")
    def search(q: str = "", view: str = "all", scope: str = "library", ask: str = "off") -> dict:
        """`q` uses the same syntax as `refs find`. `view` is a sidebar preset;
        `scope=all` also lists course material, not just library works.
        `ask`: "off" while typing (local only); "auto" when Enter is pressed,
        which asks Haiku if the query reads like a description and the local
        results look unsure, or if it starts with "?" (ask.py)."""
        all_works = works.load_all_readonly(cfg)
        note_keys = notes.existing_citekeys(cfg)
        if view == "wanted":
            items = [stub_item(w, note_keys) for w in all_works.values() if not w["files"]]
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
        with db() as conn:
            try:
                outcome = asking.search(cfg, conn, terms, filters, mode=mode, keep=keep)
            except ValueError as error:
                return {"items": [], "error": str(error)}
        found = outcome.results
        if view == "queue" and not terms:          # up-next first, then oldest queued first
            found = sorted(found, key=lambda i: (user(i).get("queue_priority") != "next",
                                                 user(i).get("queued") or ""))
        items = [shape(item, all_works, note_keys) | {"why": item.get("why")} for item in found[:800]]
        return {"items": items, "asked": outcome.asked, "message": outcome.message}

    def inbox_waiting(conn, all_works: dict | None = None) -> int | None:
        if not cfg.inboxes and not shared.folders(cfg):
            return None
        count, unseen = inbox.waiting(cfg, conn, all_works=all_works)
        if unseen:
            warm_inbox()
        return count

    inbox_warming = threading.Lock()

    def warm_inbox() -> None:
        """Judge the inbox folders' new PDFs in the background, one pass at a time."""
        if not inbox_warming.acquire(blocking=False):
            return

        def run() -> None:
            try:
                with closing(index.connect(cfg)) as conn:
                    inbox.listing(cfg, conn)
            finally:
                inbox_warming.release()
        threading.Thread(target=run, daemon=True).start()

    def downloads_waiting(conn) -> int | None:
        if downloads.folder(cfg) is None:
            return None
        count, unseen = downloads.waiting(cfg, conn)
        if unseen:
            warm_downloads()
        return count

    @app.get("/api/sidebar")
    def sidebar() -> dict:
        """Counts for the left pane: presets, then works per course and project."""
        with db() as conn:
            all_works = works.load_all_readonly(cfg)
            everything = index.search(conn, [], {}, limit=None)   # read once: renames and copies reuse it
            library_items = [i for i in everything if i["work_id"]]
            contexts: dict[str, set] = {}
            fields: dict[str, int] = {}
            slug_of: dict[str, str | None] = {}                   # context key -> its folder's name
            read_counts = {status: 0 for status in reading.RANK}
            review_count = 0
            for item in library_items:
                wid = item["work_id"]
                for loc in item["locations"]:
                    contexts.setdefault(loc["context"], set()).add(wid)
                    slug_of.setdefault(loc["context"], loc["slug"])
                for tag in item["tags"][:2]:           # the first tags are the broad fields
                    fields[tag] = fields.get(tag, 0) + 1
                if item["status"] == "needs-review":
                    review_count += 1
                if (work_rec := all_works.get(wid)) is not None:
                    status_val = reading.effective(work_rec)[0]
                    if status_val in read_counts:
                        read_counts[status_val] += 1
            # Each context's area and term, so the page groups without parsing keys.
            about = {row["context"]: {"area": row["area"], "term": row["term"]} for row in conn.execute(
                "SELECT context, MIN(area) AS area, MIN(term) AS term FROM paths GROUP BY context")}

            def stands_for(key: str) -> str | None:
                info = about.get(key) or {}
                found = collection.implicit(contexts_mod.Context(key, info.get("area") or "", info.get("term"),
                                                                 slug_of.get(key)))
                return found[0] if found else None

            return {
                "presets": {
                    "all": len(library_items),
                    # Works by read status (yours, else detected): unread, reading, read.
                    **read_counts,
                    # Queued works, with or without a PDF.
                    "queue": sum(bool(w["user"].get("queued")) for w in all_works.values()
                                 if "meta" in w),
                    "review": review_count,
                    "wanted": sum(not w["files"] for w in all_works.values()),
                    "renames": len(rename.plan(cfg, conn, everything, all_works)[0]),
                    "copies": len(organize.copies(cfg, conn, items=everything)),
                    # PDFs in Downloads that look like papers and are not in refs
                    # (None = no Downloads folder: the entry is hidden).
                    "downloads": downloads_waiting(conn),
                    # Papers in inbox and shared folders the library lacks (None:
                    # neither kind of folder is set, so the entry is hidden).
                    "inbox": inbox_waiting(conn, all_works),
                    "updates": len(updates.load(cfg)["items"]),
                    "unprocessed": conn.execute(
                        "SELECT COUNT(*) FROM files WHERE status IS NULL "
                        "AND hash IN (SELECT hash FROM paths)").fetchone()[0],
                },
                # Folders; `collection` names the collection a folder stands for
                # (listed under courses and projects instead).
                "contexts": [{"key": key, "count": len(ids), **about.get(key, {}), "collection": stands_for(key)}
                             for key, ids in sorted(contexts.items())],
                "collections": collection.listing(cfg, conn, all_works),
                # Areas the sidebar lists by name, in order (others go under "Other").
                "areas": contexts_mod.layout_for(cfg).areas,
                "fields": sorted(fields.items(), key=lambda kv: -kv[1])[:12],
                # The quick-add folder's name, for the page's hints ("save it into …").
                "dump": cfg.dump,
                # The library proxy, for a "through the library" link on papers without a PDF.
                "proxy": cfg.library_proxy,
            }

    @app.get("/api/work/{work_id}")
    def work(work_id: str) -> dict:
        found = works.load_one(cfg, work_id)
        if found is None or "meta" not in found:
            raise HTTPException(404)
        with db() as conn:
            work_copies = annotations.copies(cfg, conn, work_id)
            work_highlights = highlights.for_work(cfg, conn, work_id)
        return {"work": found, "bibtex": bib.entry(found), "note": notes.read(cfg, found),
                # Each copy's marks and whether to flag it (annotations.py).
                "copies": work_copies,
                "highlights": work_highlights,
                "backlinks": notes.backlinks(cfg, found.get("citekey") or "")}

    @app.post("/api/work/{work_id}/highlights/pull")
    def pull_highlights_to_note(work_id: str, version: str = Body(default="", embed=True),
                                x_refs: str | None = Header(default=None)) -> dict:
        """Pull extracted PDF highlights and comments into the paper's Obsidian note."""
        guard(x_refs)
        found = works.load_one(cfg, work_id)
        if found is None:
            raise HTTPException(404)
        with db() as conn:
            items = highlights.for_work(cfg, conn, work_id)
            cols = index.update_collections(conn, found)
        if not items:
            raise HTTPException(404, "no highlights or comments found in this paper's PDF")
        try:
            note = notes.pull_highlights(cfg, found, items, version, collections=cols)
        except notes.Conflict:
            raise HTTPException(409, "the note changed in Obsidian since this page loaded it")
        except FileNotFoundError as error:
            raise HTTPException(404, str(error))
        return {"note": note, "count": len(items)}

    @app.post("/api/work/{work_id}/note/sync")
    def sync_note_properties(work_id: str, x_refs: str | None = Header(default=None)) -> dict:
        """Refresh refs-managed YAML properties on the paper's Obsidian note."""
        guard(x_refs)
        found = works.load_one(cfg, work_id)
        if found is None:
            raise HTTPException(404)
        with db() as conn:
            cols = index.update_collections(conn, found)
        try:
            note = notes.sync_front_matter(cfg, found, collections=cols)
        except FileNotFoundError as error:
            raise HTTPException(404, str(error))
        return {"note": note}

    @app.post("/api/work/{work_id}/pull")
    def pull_marks(work_id: str, path: str = Body(embed=True), into: str | None = Body(default=None, embed=True),
                   x_refs: str | None = Header(default=None)) -> dict:
        """Bring the marks of another copy (`path`) into the library copy, or
        into `into` (annotations.pull): a copy of that file is kept first."""
        guard(x_refs)
        with ingest.exclusive(cfg), db() as conn:
            try:
                result = annotations.pull(cfg, conn, work_id, path, into=into)
            except annotations.Refused as error:
                raise HTTPException(409, str(error))
            if result["added"]:
                scanner.scan(cfg, conn)          # the library copy is a new version now
        return result

    @app.get("/file")
    def file(path: str) -> FileResponse:
        """Serve one PDF, but only a path the index knows (no arbitrary files)."""
        with db() as conn:
            if conn.execute("SELECT 1 FROM paths WHERE path=?", (path,)).fetchone() is None:
                raise HTTPException(404)
        return FileResponse(cfg.absolute(path), media_type="application/pdf",
                            content_disposition_type="inline")

    # --- changing things ---------------------------------------------------------------

    @app.post("/api/work/{work_id}/read")
    def set_read(work_id: str, status: str = Body(embed=True),
                 context: str | None = Body(default=None, embed=True),
                 note: str | None = Body(default=None, embed=True),
                 x_refs: str | None = Header(default=None)) -> dict:
        guard(x_refs)
        found = works.load_one(cfg, work_id)
        if found is None or status not in reading.RANK:
            raise HTTPException(400)
        reading.set_status(found, status, context=context, note=note)
        return save_reading(found)

    @app.post("/api/work/{work_id}/reads")
    def add_read_log(work_id: str, status: str = Body(default="read", embed=True),
                     context: str | None = Body(default=None, embed=True),
                     note: str | None = Body(default=None, embed=True),
                     at: str | None = Body(default=None, embed=True),
                     x_refs: str | None = Header(default=None)) -> dict:
        """Log a reading or re-reading event on a work."""
        guard(x_refs)
        found = works.load_one(cfg, work_id)
        if found is None:
            raise HTTPException(404)
        try:
            reading.log_read(found, status, context=context, note=note, at=at)
        except ValueError as error:
            raise HTTPException(400, str(error))
        # Raise current status if this reading log exceeds it.
        cur, _ = reading.effective(found)
        if reading.RANK.get(status, 0) > reading.RANK.get(cur, 0):
            found["user"]["read_status"] = status
            found["user"]["read_set"] = reading.now()
            if status == "read":
                for k in ("queued", "queue_note", "queue_for", "queue_priority"):
                    found["user"].pop(k, None)
        return save_reading(found)

    @app.post("/api/work/{work_id}/queue")
    def set_queue(work_id: str, on: bool | None = Body(default=None, embed=True),
                  note: str | None = Body(default=None, embed=True),
                  for_collection: str | None = Body(default=None, embed=True),
                  priority: str | None = Body(default=None, embed=True),
                  x_refs: str | None = Header(default=None)) -> dict:
        """Put a work on the reading queue, take it off, or update its queue note/priority."""
        guard(x_refs)
        found = works.load_one(cfg, work_id)
        if found is None:
            raise HTTPException(404)
        if on is None:
            reading.update_queue(found, note=note, for_collection=for_collection, priority=priority)
        else:
            reading.set_queued(found, on, note=note, for_collection=for_collection, priority=priority)
        return save_reading(found)

    def save_reading(found: dict) -> dict:
        """Write a work's read status or queue change, and copy it into the
        index so `read:` and `queued:` searches see it (no rescan needed)."""
        records.write_json(works.works_dir(cfg) / f"{found['id']}.json", found)
        with db() as conn:
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
        found = works.load_one(cfg, work_id)
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
        with db() as conn:
            scanner.scan(cfg, conn)       # runs works.sync and rebuilds search
        library_changed()
        return {"work": works.load_one(cfg, found["id"])}

    def library_changed() -> None:
        """Works or collections changed: library.bib follows in a few seconds."""
        if watcher is not None and watcher.exporter is not None:
            watcher.exporter.poke()

    # --- collections (collection.py) ------------------------------------------------------

    @app.get("/api/collections")
    def collections_list() -> dict:
        """Every collection, for the picker and the sidebar."""
        with db() as conn:
            return {"collections": collection.listing(cfg, conn)}

    @app.post("/api/collections/change")
    def collections_change(work_ids: list[str] = Body(embed=True), name: str = Body(embed=True),
                           add: bool = Body(default=True, embed=True),
                           x_refs: str | None = Header(default=None)) -> dict:
        """Put works into a collection (a new name makes a new collection), or
        take them out. Returns each work's collections afterwards.

        A shared collection (shared.py) also gets a copy of each paper in its
        folder (`copied`); taking papers out lists their copies there
        (`shared_copies`) for the page to offer to trash. Nothing is deleted here.
        """
        guard(x_refs)
        collections_moved()                  # a collection made shared elsewhere (the CLI)
        try:
            name = collection.clean_name(name)
            changed = collection.change(cfg, work_ids, name, add=add)
        except collection.Invalid as error:
            raise HTTPException(400, str(error))
        with db() as conn:
            copied, not_copied, shared_copies = [], [], []
            if add and name in shared.folders(cfg):
                with ingest.exclusive(cfg):
                    result = shared.copy_in(cfg, conn, work_ids, name)
                    if result["copied"]:
                        scanner.scan(cfg, conn)
                copied, not_copied = result["copied"], result["refused"]
            elif not add:
                shared_copies = shared.copies_of(cfg, conn, changed, name)
            unique_ids = list(dict.fromkeys(work_ids))
            all_works = works.load_all_readonly(cfg) if len(unique_ids) > 20 else None
            after = {}
            for work_id in unique_ids:
                work_rec = all_works.get(work_id) if all_works is not None else works.load_one(cfg, work_id)
                if work_rec is not None:
                    after[work_id] = {"collections": index.update_collections(conn, work_rec),
                                      "own_collections": collection.explicit(work_rec)}
        if changed:
            library_changed()
        return {"name": name, "changed": changed, "works": after,
                "copied": [{"work_id": w, "path": p} for w, p in copied],
                "not_copied": [{"work_id": w, "why": why} for w, why in not_copied],
                "shared_copies": shared_copies}

    def collections_moved() -> None:
        """The registry changed: if that changed which folders are shared (and
        so watched), use the new settings at once."""
        fresh = config.load()
        if fresh.shared_folders != cfg.shared_folders:
            apply_settings(fresh)

    def existing(name: str) -> str:
        """A collection's name as refs stores it, refusing one that does not
        exist (so a delete or rename never reports success for nothing)."""
        name = collection.clean_name(name)
        with db() as conn:
            known = {c["name"] for c in collection.listing(cfg, conn)}
        if name not in known:
            raise collection.Invalid(f"there is no collection #{name}")
        return name

    @app.post("/api/collections/delete")
    def collections_delete(name: str = Body(embed=True), x_refs: str | None = Header(default=None)) -> dict:
        """Forget a collection: its registry entry and your memberships (the
        page offers this for an empty one)."""
        guard(x_refs)
        try:
            name = existing(name)
            count = collection.delete(cfg, name)
        except collection.Invalid as error:
            raise HTTPException(400, str(error))
        with db() as conn:
            index.refresh_derived(cfg, conn)
            conn.commit()
        library_changed()
        collections_moved()
        return {"name": collection.clean_name(name), "papers": count}

    @app.post("/api/collections/new")
    def collections_new(name: str = Body(embed=True), kind: str = Body(default="project", embed=True),
                        term: str | None = Body(default=None, embed=True),
                        folder: str | None = Body(default=None, embed=True),
                        shared: bool = Body(default=False, embed=True),
                        bib_file: str | None = Body(default=None, embed=True),
                        make_folder: bool = Body(default=False, embed=True),
                        x_refs: str | None = Header(default=None)) -> dict:
        """A new collection (or new details for one): `refs project new` on the page."""
        guard(x_refs)
        # None leaves the folder as it is; "" (the edit form, emptied) clears it.
        folder = folder.strip() if folder is not None else None
        if shared and not folder:
            raise HTTPException(400, "a shared collection needs its shared folder")
        if make_folder and not folder:
            raise HTTPException(400, "say which folder to make")
        try:
            name = collection.clean_name(name)
            if term and not contexts_mod.term_of(term):
                raise collection.Invalid(f"{term!r} is not a term (say 2026-fall)")
            if make_folder:
                # The same checks as a new folder from "file to…": inside the
                # watched folders, a place refs may write. Made before the
                # collection is saved, so a refused folder saves nothing.
                folder = organize.make_folder(cfg, folder, anywhere=shared)
            entry = collection.register(cfg, name, kind=kind, term=term or "", folder=folder,
                                        shared=shared, bib_file=(bib_file or "").strip())
        except collection.Invalid as error:
            raise HTTPException(400, str(error))
        except organize.Refused as error:
            raise refused(error)
        library_changed()
        collections_moved()                    # a shared folder is watched from now on
        watched = bool(entry["folder"]) and cfg.is_watched(entry["folder"])
        return {"name": name, "entry": entry, "watched": watched}

    @app.post("/api/collections/arrange")
    def collections_arrange(names: list[str] = Body(embed=True), reset: bool = Body(default=False, embed=True),
                            field: str = Body(default="order", embed=True),
                            x_refs: str | None = Header(default=None)) -> dict:
        """The sidebar's order of projects (or with field="pinned", of the
        pinned collections): `names` in the order wanted, or with `reset`,
        back to A–Z for those names."""
        guard(x_refs)
        try:
            if reset:
                collection.unarrange(cfg, names)
            else:
                with db() as conn:
                    names = collection.arrange(cfg, names, conn, field)
        except collection.Invalid as error:
            raise HTTPException(400, str(error))
        return {"names": names}

    @app.post("/api/collections/hide")
    def collections_hide(names: list[str] = Body(embed=True), hidden: bool = Body(default=True, embed=True),
                         x_refs: str | None = Header(default=None)) -> dict:
        """Leave collections out of the sidebar, or bring them back. Nothing
        else changes: their papers, memberships and #name searches stay."""
        guard(x_refs)
        try:
            with db() as conn:
                names = collection.set_hidden(cfg, names, hidden, conn)
        except collection.Invalid as error:
            raise HTTPException(400, str(error))
        return {"names": names, "hidden": hidden}

    @app.post("/api/collections/pin")
    def collections_pin(names: list[str] = Body(embed=True), pinned: bool = Body(default=True, embed=True),
                        x_refs: str | None = Header(default=None)) -> dict:
        """Pin collections at the top of the sidebar, or unpin them."""
        guard(x_refs)
        try:
            with db() as conn:
                names = collection.pin(cfg, names, pinned, conn)
        except collection.Invalid as error:
            raise HTTPException(400, str(error))
        return {"names": names, "pinned": pinned}

    @app.post("/api/collections/rename")
    def collections_rename(old: str = Body(embed=True), new: str = Body(embed=True),
                           x_refs: str | None = Header(default=None)) -> dict:
        """`refs collection rename` on the page: the collection, its parts and
        every paper's membership. Papers in it by their folder stay as they are."""
        guard(x_refs)
        try:
            old, new = existing(old), collection.clean_name(new)
            if old == new:
                raise collection.Invalid("that is its name already")
            with db() as conn:
                if any(collection.matches(c["name"], new) for c in collection.listing(cfg, conn)):
                    raise collection.Invalid(f"#{new} exists already")
            count = collection.rename(cfg, old, new)
        except collection.Invalid as error:
            raise HTTPException(400, str(error))
        with db() as conn:
            index.refresh_derived(cfg, conn)
            conn.commit()
        library_changed()
        collections_moved()
        return {"old": old, "name": new, "papers": count}

    @app.post("/api/collections/append-bib")
    def collections_append_bib(name: str = Body(embed=True),
                               work_ids: list[str] = Body(default=[], embed=True),
                               x_refs: str | None = Header(default=None)) -> dict:
        """Append works (or all works in the collection) to the collection's `.bib` file."""
        guard(x_refs)
        try:
            name = collection.clean_name(name)
        except collection.Invalid as error:
            raise HTTPException(400, str(error))
        entry = collection.load(cfg).get(name)
        target = collection.bib_path(cfg, entry) if entry else None
        if target is None:
            raise HTTPException(404, f"#{name} has no .bib file set (edit collection details to add one)")
        all_works = works.load_all_readonly(cfg)
        if work_ids:
            chosen = [all_works[wid] for wid in dict.fromkeys(work_ids)
                      if wid in all_works and "meta" in all_works[wid]]
        else:
            terms, filters = _split_query([f"#{name}"])
            with db() as conn:
                ids = dict.fromkeys(i["work_id"] for i in index.search(conn, terms, filters, limit=None)
                                    if i["work_id"] in all_works and "meta" in all_works[i["work_id"]])
            chosen = sorted((all_works[i] for i in ids), key=lambda w: w["citekey"] or "")
        if not chosen:
            return {"added": [], "present": [], "bib_file": str(target)}
        target.parent.mkdir(parents=True, exist_ok=True)
        added, present = bib.append(target, chosen)
        if added:
            from .. import bibkeys
            mutable = works.load_all(cfg)
            bibkeys.learn(cfg, mutable)
            for wid, w in mutable.items():
                if wid in {a["id"] for a in added}:
                    records.write_json(works.works_dir(cfg) / f"{wid}.json", w)
        return {
            "added": [w["citekey"] for w in added],
            "present": [{"citekey": w["citekey"], "key": k} for w, k in present],
            "bib_file": entry.get("bib_file") or str(target),
        }

    @app.get("/api/collections/bibtex")
    def collections_bibtex(name: str) -> dict:
        """The BibTeX of every paper in a collection (its parts included)."""
        try:
            name = collection.clean_name(name)
        except collection.Invalid as error:
            raise HTTPException(400, str(error))
        terms, filters = _split_query([f"#{name}"])
        all_works = works.load_all_readonly(cfg)
        with db() as conn:
            ids = dict.fromkeys(i["work_id"] for i in index.search(conn, terms, filters, limit=None)
                                if i["work_id"] in all_works and "meta" in all_works[i["work_id"]])
        found = sorted((all_works[i] for i in ids), key=lambda w: w["citekey"] or "")
        return {"name": name, "count": len(found), "bibtex": "\n".join(bib.entry(w) for w in found)}

    @app.post("/api/collections/open")
    def collections_open(name: str = Body(embed=True), what: str = Body(default="folder", embed=True),
                         x_refs: str | None = Header(default=None)) -> dict:
        """Show a collection's folder (or its .bib file) in Finder. Only the
        places its registry entry names, never a path sent by the page."""
        guard(x_refs)
        try:
            entry = collection.load(cfg).get(collection.clean_name(name))
        except collection.Invalid as error:
            raise HTTPException(400, str(error))
        if entry is None:
            raise HTTPException(404, "that collection has no details saved")
        place = (cfg.absolute(entry["folder"]) if what == "folder" and entry.get("folder")
                 else collection.bib_path(cfg, entry) if what == "bib" else None)
        if place is None or not place.exists():
            raise HTTPException(404, f"no {'folder' if what == 'folder' else '.bib file'} for #{name} on this Mac")
        subprocess.run(["open", "-R", str(place)])
        return {"ok": True}

    @app.post("/api/work/{work_id}/edit")
    def edit(work_id: str, fields: dict = Body(embed=True),
             x_refs: str | None = Header(default=None)) -> dict:
        """Save your corrections. They override the pipeline and are never overwritten.
        An empty `fields` removes all corrections (back to the pipeline's record)."""
        guard(x_refs)
        found = works.load_one(cfg, work_id)
        if found is None:
            raise HTTPException(404)
        clean = {k: v for k, v in fields.items() if k in works.EDITABLE and v not in ("", None)}
        if "year" in clean:
            clean["year"] = int(clean["year"])
        found["user"]["overrides"] = clean
        found["user"]["override_source"] = "you"
        found["user"]["confirmed"] = bool(clean)
        works.sync_keys(found)
        return save_work(found)

    @app.post("/api/work/{work_id}/confirm")
    def confirm(work_id: str, x_refs: str | None = Header(default=None)) -> dict:
        """Accept the record as it is: it leaves the review queue."""
        guard(x_refs)
        found = works.load_one(cfg, work_id)
        if found is None:
            raise HTTPException(404)
        found["user"]["confirmed"] = True
        return save_work(found)

    @app.post("/api/work/{work_id}/doi")
    def set_doi(work_id: str, doi: str = Body(embed=True),
                x_refs: str | None = Header(default=None)) -> dict:
        """Replace the record with the registry's record for a DOI you supply."""
        guard(x_refs)
        found = works.load_one(cfg, work_id)
        doi = doi.strip().lower().removeprefix("https://doi.org/")
        record = resolve.by_doi(doi) if found and doi else None
        if record is None:
            raise HTTPException(404, "no registry record for that DOI")
        fields = {k: v for k, v in record.to_dict().items() if k in works.EDITABLE and v}
        fields["venue"] = venues.clean(fields.get("venue"))
        found["user"]["overrides"] = {**fields, "kind": found["meta"].get("kind")}
        found["user"]["override_source"] = f"{record.source}, DOI given by you"
        found["user"]["confirmed"] = True
        works.sync_keys(found)
        return save_work(found)

    @app.post("/api/work/{work_id}/tags")
    def edit_work_tags(work_id: str, add_user: list[str] = Body(default=[], embed=True),
                       remove_user: list[str] = Body(default=[], embed=True),
                       hide_auto: list[str] = Body(default=[], embed=True),
                       restore_auto: bool = Body(default=False, embed=True),
                       x_refs: str | None = Header(default=None)) -> dict:
        """Add/remove personal tags (`user.tags`) or hide/restore model tags."""
        guard(x_refs)
        try:
            updated = works.edit_tags(cfg, work_id, add_user=add_user, remove_user=remove_user,
                                      hide_auto=hide_auto, restore_auto=restore_auto)
        except ValueError as error:
            raise HTTPException(404, str(error))
        with db() as conn:
            tags = index.update_tags(conn, updated)
        return {"work": updated, "tags": tags, "user_tags": updated["user"].get("tags") or []}

    @app.post("/api/work/{work_id}/merge")
    def merge_works(work_id: str, other_id: str = Body(embed=True),
                    x_refs: str | None = Header(default=None)) -> dict:
        """Merge `other_id` into `work_id`, keeping `work_id`'s citekey and combining files/notes/collections."""
        guard(x_refs)
        with ingest.exclusive(cfg), db() as conn:
            try:
                result = works.merge(cfg, work_id, other_id)
            except ValueError as error:
                raise HTTPException(400, str(error))
            scanner.scan(cfg, conn)
        library_changed()
        return result

    @app.post("/api/work/{work_id}/remove")
    def remove_work(work_id: str, trash_files: bool = Body(default=True, embed=True),
                    x_refs: str | None = Header(default=None)) -> dict:
        """Remove a work from the library (trash its PDFs if requested; archive its work JSON)."""
        guard(x_refs)
        with ingest.exclusive(cfg), db() as conn:
            try:
                result = works.remove(cfg, conn, work_id, trash_files=trash_files)
            except ValueError as error:
                raise HTTPException(404, str(error))
            except organize.Refused as error:
                raise refused(error)
            scanner.scan(cfg, conn)
        library_changed()
        return result

    # --- review: renames, identical copies, filing -------------------------------------

    def rename_plan(all_works: dict | None = None) -> tuple[list, list, list]:
        with db() as conn:
            loaded = works.load_all_readonly(cfg) if all_works is None else all_works
            return rename.plan(cfg, conn, index.search(conn, [], {}, limit=None), loaded)

    def refused(error: organize.Refused) -> HTTPException:
        return HTTPException(409, str(error))

    @app.get("/api/renames")
    def renames() -> dict:
        """Every file whose name differs from the house convention, outside
        protected folders. Nothing is renamed until you approve it."""
        all_works = works.load_all_readonly(cfg)
        proposals, copies, protected = rename_plan(all_works)

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
        with ingest.exclusive(cfg), db() as conn:
            proposals = [p for p in rename.plan(cfg, conn, index.search(conn, [], {}, limit=None),
                                                works.load_all_readonly(cfg))[0] if p.path in wanted]
            done = rename.apply_many(cfg, proposals)
            scanner.scan(cfg, conn)
        return {"done": len(done)}

    @app.post("/api/undo")
    def undo(count: int = Body(embed=True), x_refs: str | None = Header(default=None)) -> dict:
        """Reverse the last `count` renames or moves (the journal is shared with the CLI)."""
        guard(x_refs)
        with ingest.exclusive(cfg), db() as conn:
            restored = rename.undo(cfg, max(0, min(count, 500)))
            scanner.scan(cfg, conn)
        return {"restored": len(restored)}

    @app.get("/api/copies")
    def copies() -> dict:
        """Papers stored more than once, each with which copy to keep and what
        to do with the others (keep.py); and the sets kept on purpose."""
        with db() as conn:
            all_works = works.load_all_readonly(cfg)
            everything = index.search(conn, [], {}, limit=None)
            supersedes = dict(conn.execute("SELECT hash, supersedes FROM files").fetchall())

            def rows(found, advise: bool):
                out = []
                for item in found:
                    row = shape(item, all_works) | {"hash": item["hash"]}
                    if advise:
                        row["advice"] = keeping.recommend(
                            cfg, conn, all_works.get(item["work_id"] or ""),
                            [loc["path"] for loc in item["locations"]],
                            supersedes=supersedes,
                        )
                    out.append(row)
                return out

            return {"items": rows(organize.copies(cfg, conn, items=everything), True),
                    "kept": rows(organize.copies(cfg, conn, kept=True, items=everything), False)}

    @app.post("/api/copies/apply")
    def copies_apply(work_ids: list[str] = Body(embed=True), optional: bool = Body(default=False, embed=True),
                     x_refs: str | None = Header(default=None)) -> dict:
        """Do what the recommendation says for these papers: trash the copies
        it marks "trash" (and "trash?" when `optional`), then give the kept
        copy its tidy name. Recomputed here, never taken from the page."""
        guard(x_refs)
        unique_ids = list(dict.fromkeys(work_ids))
        all_works = works.load_all(cfg) if len(unique_ids) > 20 else None
        trashed, renamed, refused = [], [], []
        with ingest.exclusive(cfg), db() as conn:
            for work_id in unique_ids:
                work = all_works.get(work_id) if all_works is not None else works.load_one(cfg, work_id)
                if work is None:
                    continue
                result = keeping.apply(cfg, conn, work, library.paths_of(conn, work_id), optional=optional)
                trashed += result["trashed"]
                renamed += [result["renamed"]] if result["renamed"] else []
                refused += [{"path": p, "why": why} for p, why in result["refused"]]
            if trashed or renamed:
                scanner.scan(cfg, conn)
        if trashed or renamed:
            library_changed()
        return {"trashed": trashed, "renamed": renamed, "refused": refused}

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
        with ingest.exclusive(cfg), db() as conn:
            try:
                organize.trash(cfg, conn, path)
            except organize.Refused as error:
                raise refused(error)
            scanner.scan(cfg, conn)
        return {"ok": True}

    @app.get("/api/folders")
    def folders() -> dict:
        """Where a PDF can be filed (for the "file to…" picker)."""
        with db() as conn:
            return {"folders": organize.destinations(cfg, conn)}

    @app.post("/api/move")
    def move(path: str = Body(embed=True), folder: str = Body(embed=True),
             x_refs: str | None = Header(default=None)) -> dict:
        """File a PDF into another folder. Undo with the rename journal."""
        guard(x_refs)
        with ingest.exclusive(cfg), db() as conn:
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
        with ingest.exclusive(cfg), db() as conn:
            try:
                new_path = organize.copy(cfg, conn, path, folder)
            except organize.Refused as error:
                raise refused(error)
            scanner.scan(cfg, conn)
        return {"path": new_path}

    @app.post("/api/file-many")
    def file_many(paths: list[str] = Body(embed=True), folder: str = Body(embed=True),
                  mode: str = Body(default="move", embed=True), create: bool = Body(default=False, embed=True),
                  x_refs: str | None = Header(default=None)) -> dict:
        """File (mode "move") or copy many PDFs into one folder, in one go;
        `create` makes the folder first. One scan at the end, however many
        files. `done` is also the number of undo steps that reverse it."""
        guard(x_refs)
        if mode not in {"move", "copy"}:
            raise HTTPException(400, "mode is move or copy")
        with ingest.exclusive(cfg), db() as conn:
            try:
                folder = organize.make_folder(cfg, folder) if create else folder.strip("/")
            except organize.Refused as error:
                raise refused(error)
            done, failed = organize.file_many(cfg, conn, paths, folder, mode)
            if done:
                scanner.scan(cfg, conn)
        return {"folder": folder, "done": [{"from": a, "to": b} for a, b in done],
                "refused": [{"path": p, "why": why} for p, why in failed]}

    # --- PDFs in Downloads, outside the library (downloads.py) ---------------------------

    warming = threading.Lock()

    def warm_downloads() -> None:
        """Look at new PDFs in Downloads in the background (one pass at a time),
        so the sidebar count and the view are ready without making either wait."""
        if not warming.acquire(blocking=False):
            return

        def run() -> None:
            try:
                downloads.warm(cfg)
            finally:
                warming.release()
        threading.Thread(target=run, daemon=True).start()

    @app.get("/api/downloads")
    def downloads_list() -> dict:
        """The PDFs directly in the Downloads folder, newest first, each with
        whether it looks like a paper and whether refs has it already."""
        with db() as conn:
            found = downloads.listing(cfg, conn)
        return found | {"setting": str(cfg.downloads_folder or ""), "dump": cfg.dump,
                        "where": settings._home(found["folder"]) if found["folder"] else ""}

    @app.post("/api/downloads/take")
    def downloads_take(names: list[str] = Body(embed=True), folder: str | None = Body(default=None, embed=True),
                       mode: str = Body(default="move", embed=True), create: bool = Body(default=False, embed=True),
                       queue: bool = Body(default=False, embed=True),
                       collections: list[str] = Body(default=[], embed=True),
                       x_refs: str | None = Header(default=None)) -> dict:
        """Move (or copy) PDFs from Downloads into the quick-add folder (no
        `folder`) or into a library folder (`create` makes it first), then
        read them, as one job. PDFs refs has already are left in Downloads
        and reported in `known`. Names only: nothing outside Downloads, and
        nothing in its subfolders, can be named."""
        guard(x_refs)
        with ingest.exclusive(cfg), db() as conn:              # the watcher must not read them half-moved
            try:
                if create and folder:
                    folder = organize.make_folder(cfg, folder)
                result = downloads.take(cfg, conn, names, folder, mode)
            except organize.Refused as error:     # downloads.Refused is one too
                raise refused(error)
        taken = result["taken"]
        job = None
        if taken:
            label = taken[0][1].split("/")[-1] if len(taken) == 1 else f"{len(taken)} PDFs from Downloads"
            job = start_add([str(cfg.absolute(rel)) for _, rel in taken], label, queue,
                            collections=collections)["job"]
        # Already in refs: put those papers in the collections too.
        collect_known([path for _, path in result["known"] if "/" in path], collections)
        return {"folder": result["folder"], "job": job, "mode": mode,
                "taken": [{"name": n, "to": rel} for n, rel in taken],
                "known": [{"name": n, "path": p} for n, p in result["known"]],
                "refused": [{"name": n, "why": why} for n, why in result["refused"]]}

    @app.post("/api/downloads/trash")
    def downloads_trash(names: list[str] = Body(embed=True),
                        x_refs: str | None = Header(default=None)) -> dict:
        """Move to the Trash the named PDFs in Downloads that refs already has,
        byte for byte. Anything refs does not have is refused and stays."""
        guard(x_refs)
        try:
            with db() as conn:
                result = downloads.trash_known(cfg, conn, names)
        except organize.Refused as error:
            raise refused(error)
        return {"trashed": [{"name": n, "path": p} for n, p in result["trashed"]],
                "refused": [{"name": n, "why": why} for n, why in result["refused"]]}

    @app.post("/api/open")
    def open_file(path: str = Body(embed=True), reveal: bool = Body(default=False, embed=True),
                  x_refs: str | None = Header(default=None)) -> dict:
        """Open a PDF in the default viewer, or show it in Finder."""
        guard(x_refs)
        with db() as conn:
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
            def _scan_bg() -> None:
                with closing(index.connect(new_cfg)) as bg_conn:
                    scanner.scan(new_cfg, bg_conn)
            threading.Thread(target=_scan_bg, daemon=True).start()

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
        """Annotation author names in the library's PDFs."""
        with db() as conn:
            return {"authors": settings.annotation_authors(cfg, conn)}

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
        with db() as conn:
            report = scanner.scan(cfg, conn)
        return {"seen": report.seen, "changes": report.changes}

    @app.post("/api/add")
    def add(thing: str = Body(embed=True), queue: bool = Body(default=False, embed=True),
            doi: str | None = Body(default=None, embed=True), pdf_url: str | None = Body(default=None, embed=True),
            collections: list[str] = Body(default=[], embed=True),
            x_refs: str | None = Header(default=None)) -> dict:
        """Quick add. Runs the same code as `refs add`, as a separate process,
        because it can take a while (download, model, registry). `queue` also
        puts the paper on the reading queue; `doi` and `pdf_url` are what a
        page said about itself (the bookmarklet's window, without its key)."""
        guard(x_refs)
        hints = {"doi": doi, "pdf_url": pdf_url,
                 "source_url": thing if thing.lower().startswith("http") else None}
        return start_add(thing, thing, queue, hints if doi or pdf_url else None, collections)

    def _is_pdf_bytes(body: bytes) -> bool:
        """True if `body` is a PDF (allows leading BOM/whitespace per PDF spec §7.5.2)."""
        return body.startswith(b"%PDF") or b"%PDF-" in body[:1024]

    def _verify_bookmarklet_token(token: str | None) -> None:
        if not token or not secrets.compare_digest(token.encode(), capture.token(cfg).encode()):
            raise HTTPException(403, "this bookmarklet is out of date: drag it to the bookmarks bar "
                                     "again from the refs bookmarklet page")

    # Rendezvous store for bookmarklet captures keyed by `cid` (pruned after 10 min)
    # and saved defaults (queue + collections) synced from /capture.
    captures: dict[str, dict] = {}
    captures_lock = threading.Lock()

    def _prefs_path() -> Path:
        return cfg.index_db.parent / "capture-prefs.json"

    def _load_capture_prefs() -> dict:
        try:
            data = json.loads(_prefs_path().read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return {"queue": bool(data.get("queue")),
                        "collections": [str(c).strip().lstrip("#") for c in (data.get("collections") or []) if str(c).strip()]}
        except (OSError, ValueError):
            pass
        return {"queue": False, "collections": []}

    def _save_capture_prefs(queue: bool, collections: list[str]) -> dict:
        prefs = {"queue": bool(queue),
                 "collections": [str(c).strip().lstrip("#") for c in collections if str(c).strip()]}
        try:
            _prefs_path().parent.mkdir(parents=True, exist_ok=True)
            _prefs_path().write_text(json.dumps(prefs), encoding="utf-8")
        except OSError:
            pass
        return prefs

    def _prune_captures() -> None:
        cutoff = time.time() - 600
        for k in [cid for cid, v in captures.items() if v.get("at", 0) < cutoff]:
            captures.pop(k, None)

    def _ingest_captured_pdf(body: bytes, url: str, meta: dict, queue: bool,
                             collections: list[str], cid: str | None = None) -> dict:
        """Save and add a PDF handed over by the bookmarklet, deduplicating on `cid` and SHA-256."""
        if cid:
            with captures_lock:
                _prune_captures()
                if cid in captures:
                    return captures[cid]["result"]
        if not _is_pdf_bytes(body):
            raise HTTPException(400, "what the page gave is not a PDF")
        hints = {"doi": meta.get("doi") or None, "pdf_url": None,
                 "source_url": url if url.lower().startswith(("http://", "https://")) else None}
        label = str(meta.get("title") or url or "paper.pdf")[:160]
        with db() as conn:
            known = conn.execute(
                "SELECT p.path, f.status FROM paths p JOIN files f USING(hash) WHERE p.hash=?",
                (hashlib.sha256(body).hexdigest(),)).fetchone()
        if known is not None and known["status"] is not None:
            used = collect_known([known["path"]], collections)
            result = {"ok": True, "bytes": len(body), "title": label,
                      "message": f"Already in the library: {known['path']}"
                                 + (f"; now in {', '.join('#' + n for n in used)}" if used else "")}
        elif known is not None:
            result = start_add(str(cfg.absolute(known["path"])), label, queue, hints, collections) | {
                "ok": True, "bytes": len(body), "title": label}
        else:
            dump_dir = cfg.absolute(cfg.dump)
            dump_dir.mkdir(parents=True, exist_ok=True)
            path = capture.save(body, dump_dir)
            result = start_add(str(path), label, queue, hints, collections) | {
                "ok": True, "bytes": len(body), "title": label}
        if cid:
            with captures_lock:
                captures[cid] = {"at": time.time(), "result": result}
        return result

    def _parse_multipart(content_type: str, body: bytes) -> dict[str, bytes]:
        header = f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode("latin-1", "ignore")
        msg = BytesParser(policy=email_policy).parsebytes(header + body)
        parts: dict[str, bytes] = {}
        if msg.is_multipart():
            for part in msg.iter_parts():
                name = part.get_param("name", header="content-disposition")
                if name:
                    parts[name] = part.get_payload(decode=True) or b""
        return parts

    @app.post("/api/capture-prefs")
    def set_capture_prefs(queue: bool = Body(default=False, embed=True),
                          collections: list[str] = Body(default=[], embed=True),
                          x_refs: str | None = Header(default=None)) -> dict:
        """Remember the capture window's queue & collection defaults for direct bookmarklet uploads."""
        guard(x_refs)
        return _save_capture_prefs(queue, collections)

    @app.options("/api/capture-upload")
    def capture_upload_preflight(request: Request) -> Response:
        return Response(status_code=204, headers=_cors_headers(request))

    @app.post("/api/capture-upload")
    async def capture_upload_direct(request: Request, cid: str = "", url: str = "",
                                    doi: str = "", title: str = "",
                                    token: str = "",
                                    x_refs_token: str | None = Header(default=None)) -> JSONResponse:
        """Direct token-authenticated raw PDF upload from the bookmarklet.

        Bypasses Cross-Origin-Opener-Policy (COOP) and popup blockers, and streams
        raw PDF bytes with zero base64 overhead. Authenticated by the Mac's secret key.
        """
        headers = _cors_headers(request)
        try:
            _verify_bookmarklet_token(x_refs_token or token)
            body = await request.body()
            prefs = _load_capture_prefs()
            meta = {"doi": doi or None, "title": title or None}
            result = _ingest_captured_pdf(body, url, meta, prefs["queue"], prefs["collections"], cid=cid or None)
            return JSONResponse(result, headers=headers)
        except HTTPException as exc:
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=headers)

    @app.post("/api/capture-raw")
    async def capture_raw(request: Request, cid: str = "", url: str = "", doi: str = "",
                          title: str = "", queue: bool = False, to: str = "",
                          token: str = "",
                          x_refs: str | None = Header(default=None),
                          x_refs_token: str | None = Header(default=None)) -> dict:
        """Same-origin raw binary PDF upload from /capture (zero base64 overhead)."""
        guard(x_refs)
        _verify_bookmarklet_token(x_refs_token or token)
        body = await request.body()
        cols = [n.strip().lstrip("#") for n in to.split(",") if n.strip()]
        meta = {"doi": doi or None, "title": title or None}
        return _ingest_captured_pdf(body, url, meta, queue, cols, cid=cid or None)

    @app.post("/capture-form")
    async def capture_form(request: Request) -> RedirectResponse:
        """Multipart <form> fallback when a site has BOTH strict COOP and strict connect-src CSP."""
        body = await request.body()
        parts = _parse_multipart(request.headers.get("content-type", ""), body)
        token = parts.get("token", b"").decode("utf-8", "ignore").strip()
        _verify_bookmarklet_token(token)
        cid = parts.get("cid", b"").decode("utf-8", "ignore").strip() or secrets.token_hex(6)
        url = parts.get("url", b"").decode("utf-8", "ignore").strip()
        doi = parts.get("doi", b"").decode("utf-8", "ignore").strip()
        title = parts.get("title", b"").decode("utf-8", "ignore").strip()
        pdf_bytes = parts.get("pdf", b"")
        prefs = _load_capture_prefs()
        _ingest_captured_pdf(pdf_bytes, url, {"doi": doi or None, "title": title or None},
                             prefs["queue"], prefs["collections"], cid=cid)
        return RedirectResponse(f"/capture?cid={cid}", status_code=303)

    @app.options("/api/capture-status/{cid}")
    def capture_status_preflight(cid: str, request: Request) -> Response:
        return Response(status_code=204, headers=_cors_headers(request))

    @app.get("/api/capture-status/{cid}")
    def capture_status(cid: str, request: Request, token: str = "",
                       x_refs_token: str | None = Header(default=None)) -> JSONResponse:
        """Rendezvous status for /capture?cid=... (same-origin) or bookmarklet toast (token)."""
        origin = request.headers.get("origin")
        headers = _cors_headers(request) if origin else {}
        if origin and not _refs_origin(origin):
            try:
                _verify_bookmarklet_token(x_refs_token or token)
            except HTTPException as exc:
                return JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=headers)
        with captures_lock:
            entry = captures.get(cid)
        if entry is None:
            return JSONResponse({"found": False}, headers=headers)
        res = dict(entry["result"])
        job_info = None
        if job_id := res.get("job"):
            found_job = jobs.get(job_id)
            if found_job:
                job_info = {**found_job, "seconds": int(time.time() - found_job["started"])}
        return JSONResponse({"found": True, **res, "job_info": job_info}, headers=headers)

    @app.post("/api/capture")
    def capture_add(token: str = Body(embed=True), url: str = Body(embed=True),
                    meta: dict = Body(default={}, embed=True), pdf: str | None = Body(default=None, embed=True),
                    queue: bool = Body(default=False, embed=True),
                    collections: list[str] = Body(default=[], embed=True),
                    cid: str | None = Body(default=None, embed=True),
                    x_refs: str | None = Header(default=None)) -> dict:
        """What the bookmarklet read in your browser: the page address, its
        citation tags (`meta`: doi, title, pdf_url) and, when the browser could
        fetch it signed in as you, the PDF itself (`pdf`, base64).

        Starts at once, without Enter, but only with the bookmarklet's key:
        any website can open the refs window, none can know the key."""
        guard(x_refs)
        _verify_bookmarklet_token(token)
        hints = {"doi": meta.get("doi") or None, "pdf_url": meta.get("pdf_url") or None,
                 "source_url": url if url.lower().startswith(("http://", "https://")) else None}
        label = str(meta.get("title") or url)[:160]
        if not pdf:
            # No PDF from the browser (paywalled, or another site serves it):
            # refs tries the page's PDF link, then an open copy, else saves
            # the paper without a file. It never re-fetches the page.
            if not (hints["doi"] or hints["pdf_url"] or capture.parse(url).ids):
                hints = {"source_url": hints["source_url"]}       # nothing read: let refs read the page
            return start_add(url, label, queue, hints, collections)
        try:
            body = base64.b64decode(pdf, validate=True)
        except (binascii.Error, ValueError):
            raise HTTPException(400, "the PDF did not arrive intact")
        return _ingest_captured_pdf(body, url, meta, queue, collections, cid=cid)

    @app.get("/api/inbox")
    def inbox_list() -> dict:
        """Papers in inbox folders (course folders) and shared folders that the
        library lacks, each with the collection it would go in (inbox.py)."""
        with db() as conn:
            return inbox.listing(cfg, conn)

    @app.post("/api/inbox/take")
    def inbox_take(choices: list[dict] = Body(embed=True), extra: list[str] = Body(default=[], embed=True),
                   x_refs: str | None = Header(default=None)) -> dict:
        """Bring inbox PDFs into the library: moved, or copied from shared and
        managed folders, under their canonical names (journaled; undo with u).
        A known paper goes straight into its collections (`choices[].collections`
        plus `extra`); a PDF never read is read as a new one, then filed."""
        guard(x_refs)
        with ingest.exclusive(cfg), db() as conn:              # the watcher must not read them half-moved
            result = inbox.take(cfg, conn, choices)
            if result["taken"]:
                scanner.scan(cfg, conn)
        jobs_started, unread = [], {}
        with db() as conn:
            for _, new_rel, work_id, names in result["taken"]:
                names = list(dict.fromkeys([*names, *extra]))
                if work_id:
                    shared.collect(cfg, conn, [work_id], names)
                else:                                # read it first; `refs add --to` files it
                    unread.setdefault(tuple(names), []).append(str(cfg.absolute(new_rel)))
        for names, paths in unread.items():
            label = Path(paths[0]).name if len(paths) == 1 else f"{len(paths)} PDFs from the inbox"
            jobs_started.append(start_add(paths, label, collections=list(names))["job"])
        if result["taken"]:
            library_changed()
        return {"taken": [{"from": a, "to": b, "work_id": w, "collections": n} for a, b, w, n in result["taken"]],
                "refused": [{"path": p, "why": why} for p, why in result["refused"]],
                "jobs": jobs_started}

    @app.post("/api/identify")
    def identify(path: str = Body(embed=True), x_refs: str | None = Header(default=None)) -> dict:
        """Run the pipeline on a PDF that is indexed but was never identified."""
        guard(x_refs)
        with db() as conn:
            if conn.execute("SELECT 1 FROM paths WHERE path=?", (path,)).fetchone() is None:
                raise HTTPException(404)
        return start_add(str(cfg.absolute(path)), Path(path).name)

    @app.post("/api/reread")
    def reread_pdfs(work_ids: list[str] = Body(default=[], embed=True),
                    paths: list[str] = Body(default=[], embed=True),
                    clear_overrides: bool = Body(default=False, embed=True),
                    x_refs: str | None = Header(default=None)) -> dict:
        """Re-read one or many PDFs through Claude and fresh registry lookups to
        repopulate their metadata. Preserves citekeys, collections, reading log and notes."""
        guard(x_refs)
        targets: list[tuple[str | None, str, str]] = []
        seen_hashes: set[str] = set()
        skipped_no_pdf = 0
        lib_prefix = library.folder(cfg) + "/"
        with db() as conn:
            for wid in dict.fromkeys(work_ids):
                rows = conn.execute(
                    "SELECT p.path, f.hash FROM paths p JOIN files f ON f.hash = p.hash "
                    "WHERE f.work_id = ? ORDER BY p.path", (wid,),
                ).fetchall()
                if not rows:
                    skipped_no_pdf += 1
                    continue
                chosen = next((r for r in rows if r["path"].startswith(lib_prefix)), rows[0])
                if chosen["hash"] not in seen_hashes:
                    seen_hashes.add(chosen["hash"])
                    targets.append((wid, chosen["path"], chosen["hash"]))
            for rel in dict.fromkeys(paths):
                row = conn.execute(
                    "SELECT p.path, f.hash, f.work_id FROM paths p JOIN files f ON f.hash = p.hash "
                    "WHERE p.path = ?", (rel,),
                ).fetchone()
                if row is not None and row["hash"] not in seen_hashes:
                    seen_hashes.add(row["hash"])
                    targets.append((row["work_id"], row["path"], row["hash"]))
        if not targets:
            raise HTTPException(400, "none of the selected papers has a PDF on disk to re-read")

        job_id = secrets.token_hex(4)
        label = Path(targets[0][1]).name if len(targets) == 1 else f"{len(targets)} PDFs"
        jobs[job_id] = {"state": "running", "label": f"re-reading {label}",
                        "message": "", "started": time.time()}

        def run() -> None:
            try:
                def step(n: int, total: int, name: str) -> None:
                    jobs[job_id]["message"] = f"{n} of {total}: {name}" if total > 1 else f"reading {name}"
                with ingest.exclusive(cfg), db() as conn:
                    res = ingest.reread(cfg, conn, targets, clear_overrides=clear_overrides, progress=step)
                if res["done"]:
                    library_changed()
                if len(targets) == 1 and res["done"]:
                    rel, status, issues = res["results"][0]
                    msg = f"re-read {Path(rel).name}: {status}" + (f" ({issues[0]})" if issues else "")
                    if skipped_no_pdf:
                        msg += f" ({skipped_no_pdf} had no PDF)"
                    jobs[job_id].update(state="done", message=msg)
                elif res["done"]:
                    n_fail = len(res["failed"])
                    msg = f"re-read {res['done']} of {len(targets)} PDFs" + (
                        f" ({skipped_no_pdf} had no PDF)" if skipped_no_pdf else ""
                    ) + (
                        f"; {n_fail} failed ({res['failed'][0][1]})" if n_fail else ""
                    )
                    jobs[job_id].update(state="done", message=msg)
                else:
                    why = res["failed"][0][1] if res["failed"] else "failed"
                    jobs[job_id].update(state="failed", message=why)
            except Exception as error:  # noqa: BLE001
                jobs[job_id].update(state="failed", message=f"{type(error).__name__}: {error}")

        threading.Thread(target=run, daemon=True).start()
        return {"job": job_id, "count": len(targets), "skipped_no_pdf": skipped_no_pdf}

    @app.post("/api/upload")
    async def upload(request: Request, name: str = "upload.pdf", to: str = "",
                     x_refs: str | None = Header(default=None)) -> dict:
        """A PDF dropped on the page: save it into the dump, then add it like any
        other. `to`: collections to put it in, comma-separated (the one the
        page shows, when you drop it there)."""
        guard(x_refs)
        names = [n for n in to.split(",") if n.strip()]
        body = await request.body()
        if not _is_pdf_bytes(body):
            raise HTTPException(400, "that file is not a PDF")
        # The same PDF may already be in the library: do not store a second copy.
        with db() as conn:
            known = conn.execute(
                "SELECT p.path, f.status FROM paths p JOIN files f USING(hash) WHERE p.hash=?",
                (hashlib.sha256(body).hexdigest(),)).fetchone()
        if known is not None and known["status"] is None:
            # On disk, but never identified (an earlier attempt was interrupted).
            return start_add(str(cfg.absolute(known["path"])), Path(known["path"]).name, collections=names)
        if known is not None:
            used = collect_known([known["path"]], names)
            return {"ok": True, "message": f"Already in the library: {known['path']}"
                                          + (f"; now in {', '.join('#' + n for n in used)}" if used else "")}
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
        return start_add(str(target), target.name, collections=names)

    # --- published updates (updates.py) & batch import (batch.py) ----------------------

    @app.get("/api/updates")
    def updates_list() -> dict:
        """Working papers that have a verified published version waiting for review."""
        return updates.load(cfg)

    @app.post("/api/updates/check")
    def updates_check(x_refs: str | None = Header(default=None)) -> dict:
        """Re-check every working paper against Crossref and OpenAlex in the background."""
        guard(x_refs)
        job_id = secrets.token_hex(4)
        jobs[job_id] = {"state": "running", "label": "checking for published versions",
                        "message": "starting …", "started": time.time()}

        def run() -> None:
            try:
                def step(done: int, total: int, label: str) -> None:
                    jobs[job_id]["message"] = f"{done} of {total}: {label}"
                result = updates.check_all(cfg, refresh=True, progress=step)
                n = len(result["items"])
                msg = (f"checked {result['checked']} working papers: "
                       f"{n} published {'update' if n == 1 else 'updates'} found")
                jobs[job_id].update(state="done", message=msg)
            except Exception as error:  # noqa: BLE001
                jobs[job_id].update(state="failed", message=f"{type(error).__name__}: {error}")

        threading.Thread(target=run, daemon=True).start()
        return {"job": job_id}

    @app.post("/api/updates/apply")
    def updates_apply(work_id: str = Body(embed=True), proposed: dict | None = Body(default=None, embed=True),
                      x_refs: str | None = Header(default=None)) -> dict:
        """Upgrade a working paper's metadata to its published record."""
        guard(x_refs)
        try:
            updated = updates.apply(cfg, work_id, proposed)
        except ValueError as error:
            raise HTTPException(404, str(error))
        with db() as conn:
            scanner.scan(cfg, conn)
        library_changed()
        return {"work": updated}

    @app.post("/api/updates/ignore")
    def updates_ignore(work_id: str = Body(embed=True), key: str = Body(embed=True),
                       x_refs: str | None = Header(default=None)) -> dict:
        """Dismiss a published-version proposal for a work."""
        guard(x_refs)
        try:
            updates.ignore(cfg, work_id, key)
        except ValueError as error:
            raise HTTPException(404, str(error))
        return {"ok": True}

    @app.post("/api/import/preview")
    def import_preview(text: str = Body(embed=True), use_llm: bool = Body(default=False, embed=True),
                       resolve_online: bool = Body(default=True, embed=True),
                       x_refs: str | None = Header(default=None)) -> dict:
        """Parse a .bib, DOI/URL list, or syllabus and reconcile against the library (read-only)."""
        guard(x_refs)
        entries = batch.parse_input(text, use_llm=use_llm)
        if not entries:
            return {"items": [], "counts": {"existing": 0, "new_oa": 0, "new_stub": 0, "unresolved": 0}}
        with db() as conn:
            return batch.preview(cfg, conn, entries, resolve_online=resolve_online)

    @app.post("/api/import/commit")
    def import_commit(items: list[dict] = Body(embed=True),
                      collection_name: str | None = Body(default=None, embed=True),
                      queue: bool = Body(default=False, embed=True),
                      download_oa: bool = Body(default=True, embed=True),
                      x_refs: str | None = Header(default=None)) -> dict:
        """Commit previewed import items: tag existing papers, create verified stubs, and
        optionally download open-access PDFs sequentially in a background job."""
        guard(x_refs)
        try:
            with db() as conn:
                result = batch.commit(cfg, conn, items, collection_name=collection_name, queue=queue)
        except collection.Invalid as error:
            raise HTTPException(400, str(error))
        library_changed()

        job_id = None
        targets = result["download_targets"] if download_oa else []
        if targets:
            job_id = secrets.token_hex(4)
            jobs[job_id] = {"state": "running", "label": f"{len(targets)} open-access PDFs",
                            "message": "", "started": time.time()}

            def run_downloads() -> None:
                got = 0
                dump_dir = cfg.absolute(cfg.dump)
                dump_dir.mkdir(parents=True, exist_ok=True)
                for n, target in enumerate(targets, 1):
                    jobs[job_id]["message"] = f"{n} of {len(targets)}: {target['citekey']}"
                    try:
                        saved = capture.download_first(target["pdf_urls"], dump_dir)
                        if saved is None:
                            continue
                        opts = []
                        if target.get("doi"):
                            opts += ["--doi", str(target["doi"]), "--no-page"]
                        for col in target.get("collections") or []:
                            opts += ["--to", col]
                        done = subprocess.run([sys.executable, "-m", "refs.cli", "add", str(saved), *opts],
                                              capture_output=True, text=True, timeout=300)
                        if done.returncode == 0:
                            got += 1
                    except Exception:  # noqa: BLE001 (leave the stub cleanly behind)
                        continue
                jobs[job_id].update(
                    state="done",
                    message=f"downloaded {got} of {len(targets)} open-access PDFs (the rest stay as stubs)",
                )

            threading.Thread(target=run_downloads, daemon=True).start()

        return {"tagged": result["tagged"], "created": result["created"],
                "stubs": result["stubs"], "oa_queued": len(targets), "job": job_id}

    return app
