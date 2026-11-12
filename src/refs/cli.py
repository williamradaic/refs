"""The `refs` command.

    refs scan                 bring the index in line with Dropbox
    refs status               what the library holds
    refs find blp             search; add filters like course:220a kind:paper
    refs contexts             the courses and projects the app has recognised
    refs dupes                files stored in more than one place
    refs open blp             open the best match in the default PDF viewer
    refs path blp             print its full path (handy inside scripts)

`find`, `status`, `contexts` and `dupes` accept --json, which is what lets
coding agents use the library.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import PurePosixPath

from . import __version__, config, index
from . import scan as scanner


# --- Small display helpers -----------------------------------------------------

def _plural(n: int, word: str) -> str:
    plural = word + ("es" if word.endswith(("ch", "s", "x")) else "s")
    return f"{n:,} {word if n == 1 else plural}"


def _headline(item: dict) -> str:
    """One line describing a file: "Berry, Levinsohn, Pakes (1995) automobile prices..."."""
    authors = ", ".join(a.capitalize() if a.islower() else a for a in item["authors"])
    if not item["title"] and not item["ids"]:
        # Nothing better than the file name itself, which already says it all.
        return PurePosixPath(item["locations"][0]["path"]).stem
    title = item["title"] or ""
    if item["ids"] and not item["authors"]:
        title = " ".join(f"{k.upper()} {v}" for k, v in item["ids"].items())
    year = f" ({item['year']})" if item["year"] else ""
    return f"{authors}{year} {title}".strip() if authors else f"{title}{year}"


def _where(loc: dict) -> str:
    """"coursework/2026-fall/220a · 3. Demand estimation II"."""
    text = loc["context"] or ""
    if loc["topic"]:
        order = f"{loc['topic_order']}. " if loc["topic_order"] is not None else ""
        text += f" · {order}{loc['topic']}"
    elif loc["section"]:
        text += f" · {loc['section']}"
    return text


def _split_query(words: list[str]) -> tuple[list[str], dict[str, str]]:
    """Separate free text from `key:value` filters."""
    terms, filters = [], {}
    for word in words:
        key, sep, value = word.partition(":")
        if sep and value and key.isalpha():
            filters[key.lower()] = value
        else:
            terms.append(word)
    return terms, filters


# --- Commands --------------------------------------------------------------------

def cmd_scan(args: argparse.Namespace) -> int:
    cfg = config.load()
    conn = index.connect(cfg)

    # A progress line while hashing, only when a person is watching.
    def progress(done: int, total: int) -> None:
        if sys.stderr.isatty() and (done % 25 == 0 or done == total):
            print(f"\r  reading {done:,}/{total:,} files", end="", file=sys.stderr)
            if done == total:
                print(file=sys.stderr)

    report = scanner.scan(cfg, conn, progress)

    if args.json:
        print(json.dumps(report.__dict__, indent=2))
        return 0

    print(f"Scanned {_plural(report.seen, 'PDF')} in {report.seconds:.1f}s "
          f"({_plural(report.unchanged, 'unchanged file')}).")
    sections = [
        ("New", report.added), ("Back again", report.restored),
        ("Extra copy of a known file", report.copies),
        ("Moved or renamed", [f"{old}  ->  {new}" for old, new in report.moved]),
        ("Edited in place", report.modified), ("Gone", report.removed),
        ("Online-only, skipped", report.online_only),
    ]
    for label, items in sections:
        if not items:
            continue
        print(f"\n{label}: {len(items):,}")
        shown = items if args.verbose else items[:10]
        for item in shown:
            print(f"  {item}")
        if len(items) > len(shown):
            print(f"  ... and {len(items) - len(shown):,} more (use -v to list all)")
    if not report.changes and not report.online_only:
        print("Nothing changed.")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    cfg = config.load()
    conn = index.connect(cfg)

    one = lambda sql: conn.execute(sql).fetchone()[0]   # noqa: E731  (tiny helper)
    data = {
        "dropbox_root": str(cfg.dropbox_root),
        "roots": cfg.roots,
        "index": str(cfg.index_db),
        "last_scan": (conn.execute("SELECT value FROM meta WHERE key='last_scan'").fetchone() or [None])[0],
        "locations": one("SELECT COUNT(*) FROM paths"),
        "files": one("SELECT COUNT(DISTINCT hash) FROM paths"),
        "duplicated_files": one(
            "SELECT COUNT(*) FROM (SELECT hash FROM paths GROUP BY hash HAVING COUNT(*) > 1)"),
        "by_kind": dict(conn.execute(
            "SELECT COALESCE(kind,'unknown'), COUNT(*) FROM files "
            "WHERE hash IN (SELECT hash FROM paths) GROUP BY 1 ORDER BY 2 DESC").fetchall()),
        "by_status": dict(conn.execute(
            "SELECT COALESCE(status,'not processed'), COUNT(*) FROM files "
            "WHERE hash IN (SELECT hash FROM paths) GROUP BY 1 ORDER BY 2 DESC").fetchall()),
        "by_area": dict(conn.execute(
            "SELECT area, COUNT(DISTINCT hash) FROM paths GROUP BY 1 ORDER BY 2 DESC").fetchall()),
    }
    if args.json:
        print(json.dumps(data, indent=2))
        return 0

    if data["last_scan"] is None:
        print("The index is empty. Run `refs scan` first.")
        return 0
    print(f"Dropbox     {data['dropbox_root']}")
    print(f"Scanning    {', '.join(data['roots'])}")
    print(f"Last scan   {data['last_scan']}")
    print(f"Library     {_plural(data['files'], 'distinct file')} in "
          f"{_plural(data['locations'], 'location')} "
          f"({data['duplicated_files']:,} stored in more than one place)")
    print("\nBy kind")
    for kind, count in data["by_kind"].items():
        print(f"  {count:>6,}  {kind}")
    print("\nBy status")
    for status, count in data["by_status"].items():
        print(f"  {count:>6,}  {status}")
    print("\nBy area")
    for area, count in data["by_area"].items():
        print(f"  {count:>6,}  {area}")
    return 0


def cmd_find(args: argparse.Namespace) -> int:
    cfg = config.load()
    conn = index.connect(cfg)
    terms, filters = _split_query(args.query)
    try:
        results = index.search(conn, terms, filters, limit=None if args.all else args.limit)
    except ValueError as error:
        print(f"refs: {error}. Filters: course, project, term, area, in, topic, "
              "section, kind, year, author, status, tag.", file=sys.stderr)
        return 2

    if args.json:
        # Absolute paths included so a script can open the file directly.
        for item in results:
            for loc in item["locations"]:
                loc["absolute_path"] = str(cfg.absolute(loc["path"]))
        print(json.dumps(results, indent=2, ensure_ascii=False))
        return 0

    if not results:
        print("No matches.")
        return 1
    for item in results:
        label = item["kind"] + (f", {item['status']}" if item["status"] else ", not processed")
        print(f"{_headline(item)}   [{label}]" + (f"  @{item['citekey']}" if item["citekey"] else ""))
        if item["venue"] or item["tags"]:
            print("    " + " · ".join(filter(None, [item["venue"], ", ".join(item["tags"][:6])])))
        for loc in item["locations"]:
            print(f"    {_where(loc)}")
            print(f"      {loc['path']}")
    print(f"\n{_plural(len(results), 'match')}" + ("" if args.all else f" (showing up to {args.limit})"))
    return 0


def cmd_bib(args: argparse.Namespace) -> int:
    """Print BibTeX entries for the works a query selects."""
    from . import bib, works

    cfg = config.load()
    conn = index.connect(cfg)
    terms, filters = _split_query(args.query)
    all_works = works.load_all(cfg)
    # Only library works have BibTeX; with search words, keep the best `--limit`.
    results = [r for r in index.search(conn, terms, filters, limit=None)
               if r["work_id"] in all_works and "meta" in all_works[r["work_id"]]]
    if terms:
        results = results[:args.limit]
    entries = [bib.entry(all_works[r["work_id"]]) for r in results]
    if not entries:
        print("No library works match.", file=sys.stderr)
        return 1
    print("\n\n".join(entries))
    return 0


def cmd_contexts(args: argparse.Namespace) -> int:
    cfg = config.load()
    conn = index.connect(cfg)
    rows = conn.execute(
        "SELECT p.context, COUNT(DISTINCT p.hash) AS files, "
        "COUNT(DISTINCT CASE WHEN f.kind='paper' THEN p.hash END) AS papers "
        "FROM paths p JOIN files f ON f.hash = p.hash GROUP BY p.context ORDER BY p.context"
    ).fetchall()
    if args.json:
        print(json.dumps([dict(r) for r in rows], indent=2))
        return 0
    print(f"{'files':>6} {'papers':>7}  context")
    for row in rows:
        print(f"{row['files']:>6,} {row['papers']:>7,}  {row['context']}")
    return 0


def cmd_dupes(args: argparse.Namespace) -> int:
    cfg = config.load()
    conn = index.connect(cfg)
    results = [r for r in index.search(conn, [], {}, limit=None) if len(r["locations"]) > 1]
    if args.json:
        print(json.dumps(results, indent=2, ensure_ascii=False))
        return 0
    for item in results:
        print(f"{_headline(item)}   [{item['kind']}]")
        for loc in item["locations"]:
            print(f"    {loc['path']}")
    print(f"\n{_plural(len(results), 'file')} stored in more than one place.")
    return 0


def _best_match(args: argparse.Namespace) -> tuple[config.Config, dict | None]:
    cfg = config.load()
    conn = index.connect(cfg)
    terms, filters = _split_query(args.query)
    results = index.search(conn, terms, filters, limit=1)
    return cfg, (results[0] if results else None)


def cmd_path(args: argparse.Namespace) -> int:
    cfg, item = _best_match(args)
    if item is None:
        print("No matches.", file=sys.stderr)
        return 1
    print(cfg.absolute(item["locations"][0]["path"]))
    return 0


def cmd_open(args: argparse.Namespace) -> int:
    cfg, item = _best_match(args)
    if item is None:
        print("No matches.", file=sys.stderr)
        return 1
    target = cfg.absolute(item["locations"][0]["path"])
    print(f"Opening {_headline(item)}")
    # `open` is the macOS command behind a double-click in Finder.
    return subprocess.run(["open", str(target)]).returncode


def cmd_process(args: argparse.Namespace) -> int:
    """Run the reading + lookup pipeline on the files a query selects."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    from . import llm, pipeline

    cfg = config.load()
    conn = index.connect(cfg)
    terms, filters = _split_query(args.query)
    if not terms and not filters and not args.all:
        print("Say which files (e.g. `refs process course:220a kind:paper`), or pass --all.",
              file=sys.stderr)
        return 2
    files = index.search(conn, terms, filters, limit=None)
    # Each file is paid for once. A file is (re)done when it has no read record,
    # when an earlier attempt failed part-way, or when asked: --relookup repeats
    # only the free registry step, --redo starts from scratch.
    previous = {f["hash"]: pipeline.load_read(cfg, f["hash"]) for f in files}

    def needs_work(f: dict) -> bool:
        old = previous[f["hash"]]
        if args.relookup:                 # only files the model has already read
            return old is not None and bool(old.get("reading"))
        if old is None or args.redo:
            return True
        return any("failed" in issue for issue in old["issues"])

    todo = [f for f in files if needs_work(f)]
    if args.limit:
        todo = todo[:args.limit]
    print(f"{_plural(len(files), 'file')} selected, {len(todo):,} to process.")
    if not todo:
        return 0

    api = llm.client()
    topics = pipeline.known_topics(cfg)
    totals = {"input_tokens": 0, "output_tokens": 0}
    statuses: dict[str, int] = {}

    # A few files at a time: most of the wait is the network, not this computer.
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        jobs = {
            pool.submit(pipeline.process_file, cfg, api, f["hash"], f["locations"][0]["path"],
                        topics, None if args.redo else previous[f["hash"]]): f
            for f in todo
        }
        for job in as_completed(jobs):
            read = job.result()
            statuses[read["status"]] = statuses.get(read["status"], 0) + 1
            if not read["reused_reading"]:      # a reused reading costs nothing now
                for key in totals:
                    totals[key] += read["usage"].get(key, 0)
            meta = pipeline.final_metadata(read)
            kind = (read.get("reading") or {}).get("kind") or "?"
            who = ", ".join(meta["surnames"][:3]) + (" et al." if len(meta["surnames"]) > 3 else "")
            print(f"  {read['status']:<12} {kind:<11} {who} ({meta['year']}) {meta['title']}"
                  if meta["title"] else
                  f"  {read['status']:<12} {kind:<11} {read['path'].rsplit('/', 1)[-1]}")
            for issue in read["issues"]:
                print(f"      ! {issue}")

    print("\n" + ", ".join(f"{n} {status}" for status, n in sorted(statuses.items())))
    print(f"Tokens: {totals['input_tokens']:,} in, {totals['output_tokens']:,} out. "
          f"Cost at list price: ${llm.cost(totals):.4f} "
          f"(${llm.cost(totals) / len(todo):.5f} per file).")
    return 0


def cmd_eval(args: argparse.Namespace) -> int:
    """Compare pipeline results with file names that already follow the convention.

    The model never sees file names, so a well-named file is independent ground
    truth for surname, year and title words.
    """
    from . import naming, pipeline

    cfg = config.load()
    conn = index.connect(cfg)
    terms, filters = _split_query(args.query)
    files = index.search(conn, terms, filters, limit=None)

    counts: dict[str, int] = {}
    problems: list[str] = []
    year_notes: list[str] = []
    for f in files:
        read = pipeline.load_read(cfg, f["hash"])
        name = f["locations"][0]["path"].rsplit("/", 1)[-1]
        truth = naming.parse_filename(name)
        if read is None or truth.style != "convention" or not truth.authors:
            continue
        counts["checkable"] = counts.get("checkable", 0) + 1
        counts[f"status: {read['status']}"] = counts.get(f"status: {read['status']}", 0) + 1
        kind = (read.get("reading") or {}).get("kind")
        meta = pipeline.final_metadata(read)

        # Surnames: compare on slug pieces, so "Scott Morton" matches "scott-morton".
        got = {piece for s in meta["surnames"] for piece in naming.slugify(s).split("-")}
        named = [a for a in truth.authors if a not in {"et", "al"}]
        # Registries sometimes file "Scott Morton" under "Morton": allow one in four.
        authors_ok = bool(named) and sum(a in got for a in named) / len(named) >= 0.75
        # Title: the name holds a shortened title; its longer words should appear.
        words = [w for w in (truth.title or "").split() if len(w) > 3]
        have = set(naming.slugify(meta["title"] or "").split("-"))
        title_ok = not words or sum(w in have for w in words) / len(words) >= 0.6
        kind_ok = kind in pipeline.LIBRARY_KINDS
        year_ok = meta["year"] == truth.year

        for label, ok in (("kind", kind_ok), ("authors", authors_ok), ("title", title_ok), ("year", year_ok)):
            counts[f"{label} ok"] = counts.get(f"{label} ok", 0) + ok
        if not (kind_ok and authors_ok and title_ok):
            problems.append(
                f"  {name}\n      got [{read['status']}, {kind}] {meta['surnames']} "
                f"({meta['year']}) {meta['title']!r}  issues={read['issues']}")
        elif not year_ok:
            year_notes.append(f"  {name}: file name says {truth.year}, record says "
                              f"{meta['year']} ({meta['venue']})")

    if not counts:
        print("No processed files with convention-style names in that selection.")
        return 1
    total = counts["checkable"]
    print(f"Checked {total} files whose names follow the convention.\n")
    for label in sorted(k for k in counts if k.startswith("status")):
        print(f"  {counts[label]:>3}  {label}")
    print()
    for label in ("kind ok", "authors ok", "title ok", "year ok"):
        print(f"  {counts.get(label, 0):>3}/{total}  {label}")
    print(f"\nWrong or missing kind, authors or title: {len(problems)}")
    print("\n".join(problems))
    print(f"\nYear differs only (often working paper vs published, or the name is off): {len(year_notes)}")
    print("\n".join(year_notes))
    return 0


def cmd_add(args: argparse.Namespace) -> int:
    """Quick add: a link, identifier or local PDF goes into the dump and the library."""
    import shutil
    from pathlib import Path

    from . import bib, capture, llm, pipeline, rename, resolve, storage, works

    cfg = config.load()
    conn = index.connect(cfg)
    dump = cfg.absolute(cfg.dump)
    thing, source_url, target = args.thing, None, None

    # 1. Get a PDF into the dump: copy a local file, or download an open copy.
    local = Path(thing).expanduser()
    if local.is_file() and local.suffix.lower() == ".pdf":
        path = dump / local.name
        if local.resolve().is_relative_to(cfg.dropbox_root):
            path = local.resolve()                       # already in Dropbox: use it where it is
        elif not path.exists():
            shutil.copy2(local, path)
    else:
        target = capture.discover(capture.parse(thing))
        source_url = target.page_url or target.pdf_url
        path = capture.download(target.pdf_url, dump) if target.pdf_url else None

    # 2. No PDF could be fetched: save the paper itself, to be completed later.
    if path is None:
        record, _ = resolve.by_ids(target.ids) if target.ids else (None, None)
        if record is None:
            print("Could not identify a paper or find an open PDF from that. "
                  "Download the PDF yourself and save it into papers-dump.")
            return 1
        work = works.create_stub(cfg, record.to_dict(), source_url)
        print(f"Saved without a PDF (no open copy found): {record.title} ({record.year})")
        print(f"  @{work['citekey']} . Save the PDF into papers-dump and it will attach itself.")
        return 0

    # 3. Index the new file, run the pipeline on it, and file it under its work.
    rel = str(path.relative_to(cfg.dropbox_root))
    scanner.scan(cfg, conn)
    row = conn.execute("SELECT hash FROM paths WHERE path=?", (rel,)).fetchone()
    if row is None:
        print(f"The file was saved to {rel} but could not be indexed.")
        return 1
    read = pipeline.load_read(cfg, row["hash"]) or pipeline.process_file(
        cfg, llm.client(), row["hash"], rel, pipeline.known_topics(cfg))
    file_to_work = works.sync(cfg)
    work = works.load_all(cfg).get(file_to_work.get(row["hash"], ""))
    if work and source_url and not work["user"].get("source_url"):
        work["user"]["source_url"] = source_url
        pipeline.records.write_json(works.works_dir(cfg) / f"{work['id']}.json", work)

    # 4. Inside the dump, a verified paper gets its proper name straight away.
    if work and rel.startswith(cfg.dump + "/"):
        downloaded = path.name.startswith("added-")      # the app's own temporary name
        new_name = rename.proposed_name(work, path.name, allow_unverified=downloaded)
        rel = (new_name and rename.apply(cfg, rel, new_name, row["hash"])) or rel
    scanner.scan(cfg, conn)

    meta = pipeline.final_metadata(read)
    print(f"Added [{read['status']}]: {', '.join(meta['surnames'][:3])} ({meta['year']}) {meta['title']}")
    print(f"  {rel}" + (f"   @{work['citekey']}" if work else ""))
    for issue in read["issues"]:
        print(f"  ! {issue}")
    return 0


def cmd_rename(args: argparse.Namespace) -> int:
    """Propose (default) or apply canonical file names for the files a query selects."""
    from pathlib import PurePosixPath

    from . import rename, works

    cfg = config.load()
    conn = index.connect(cfg)
    if args.undo:
        for original, was in rename.undo(cfg, args.undo):
            print(f"restored  {was}  ->  {original}")
        scanner.scan(cfg, conn)
        return 0

    terms, filters = _split_query(args.query)
    if not terms and not filters:
        print("Say which files, e.g. `refs rename in:dump`.", file=sys.stderr)
        return 2
    all_works = works.load_all(cfg)
    from . import records

    # Collect candidates, with each file's own hash and whether it is a working paper.
    candidates, skipped = [], 0
    for item in index.search(conn, terms, filters, limit=None):
        work = all_works.get(item["work_id"] or "")
        for loc in item["locations"]:
            name = PurePosixPath(loc["path"]).name
            new_name = work and rename.proposed_name(work, name)
            if not new_name or new_name == name:
                continue
            if rename.blocked_reason(cfg, loc["path"]):
                skipped += 1
                continue
            file_hash = conn.execute("SELECT hash FROM paths WHERE path=?", (loc["path"],)).fetchone()[0]
            reading = (records.load_read(cfg, file_hash) or {}).get("reading") or {}
            candidates.append((loc["path"], new_name, file_hash, bool(reading.get("is_working_paper"))))

    # Two files in one folder can want the same name. Published versions choose
    # first; an identical copy is reported, not renamed; a different version of
    # the same paper gets a "-wp" or "-2" suffix.
    planned, copies = [], []
    taken: dict[str, dict[str, str]] = {}          # folder -> {name: hash that owns it}
    for rel, new_name, file_hash, is_wp in sorted(candidates, key=lambda c: (c[3], c[0])):
        folder = str(PurePosixPath(rel).parent)
        names = taken.setdefault(folder, {
            p.name: "" for p in cfg.absolute(folder).glob("*.pdf")})   # names already on disk
        if names.get(new_name) == file_hash:
            copies.append(rel)                     # same content as a file already planned
            continue
        final = rename.disambiguate(new_name, set(names), is_wp)
        names[final] = file_hash
        planned.append((rel, final))

    done = 0
    for rel, new_name in planned:
        if args.apply:
            done += rename.apply(cfg, rel, new_name) is not None
        else:
            print(f"{rel}\n    -> {new_name}")
    if args.apply:
        scanner.scan(cfg, conn)
        print(f"Renamed {done:,} of {len(planned):,} files. Undo with `refs rename --undo {done}`.")
    else:
        print(f"\n{_plural(len(planned), 'file')} would be renamed"
              f"{f', {skipped} protected' if skipped else ''}. Nothing was changed; add --apply to rename.")
    if copies:
        print(f"\nIdentical copies left as they are ({len(copies)}); you may want to delete them:")
        for rel in copies:
            print(f"  {rel}")
    return 0


def cmd_wanted(args: argparse.Namespace) -> int:
    """Works saved without a PDF yet."""
    from . import works

    cfg = config.load()
    waiting = [w for w in works.load_all(cfg).values() if not w["files"]]
    for work in sorted(waiting, key=lambda w: w["created"], reverse=True):
        meta = work["meta"]
        names = ", ".join(a["family"] for a in (meta.get("authors") or [])[:3])
        print(f"{names} ({meta.get('year')}) {meta.get('title')}   @{work['citekey']}")
        if work["user"].get("source_url"):
            print(f"    {work['user']['source_url']}")
    print(f"\n{_plural(len(waiting), 'work')} waiting for a PDF.")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    """Start the web interface on this computer only."""
    import uvicorn

    from .web.app import create_app

    url = f"http://127.0.0.1:{args.port}"
    print(f"refs is running at {url}  (Ctrl-C to stop)")
    if args.open:
        subprocess.Popen(["open", url])
    # host 127.0.0.1: reachable from this Mac only, not from the network.
    uvicorn.run(create_app(), host="127.0.0.1", port=args.port, log_level="warning")
    return 0


def cmd_zotero_import(args: argparse.Namespace) -> int:
    """Bring a Zotero library in: references, collections, tags, notes, citation keys."""
    import shutil
    from pathlib import Path

    from . import works, zotero

    cfg = config.load()
    conn = index.connect(cfg)
    zotero_dir = Path(args.zotero_dir).expanduser()
    if not (zotero_dir / "zotero.sqlite").exists():
        print(f"No zotero.sqlite in {zotero_dir}.", file=sys.stderr)
        return 1
    items = zotero.read_items(zotero.snapshot(zotero_dir))

    known_paths = {r["path"]: r["hash"] for r in conn.execute("SELECT path, hash FROM paths")}
    known_hashes = set(known_paths.values())
    importer = works.Importer(cfg)
    matched = unmatched = copied = 0
    for item in items:
        hashes = []
        for attachment in item["attachments"]:
            file_hash, on_disk = zotero.locate_attachment(
                cfg, zotero_dir, attachment, known_paths, known_hashes)
            if file_hash:
                hashes.append(file_hash)
                matched += 1
            elif on_disk is not None:
                unmatched += 1
                # A PDF that lives only inside Zotero: optionally copy it to the
                # dump, where the normal pipeline will pick it up and attach it.
                if args.copy_pdfs and not args.dry_run:
                    target = cfg.absolute(cfg.dump) / on_disk.name
                    if not target.exists():
                        shutil.copy2(on_disk, target)
                        copied += 1
        importer.add(
            zotero.to_record(item), file_hashes=hashes,
            external={"key": item["key"], "citekey": item["fields"].get("citationKey"),
                      "added": item["added"]},
            tags=item["tags"], collections=item["collections"], notes=item["notes"],
        )

    print(f"Zotero library at {zotero_dir}: {_plural(len(items), 'reference')} (trash excluded).")
    print(f"  {importer.joined:,} already in the library (joined to the existing work)")
    print(f"  {importer.created:,} new works")
    print(f"  PDFs: {matched:,} already in Dropbox and linked; {unmatched:,} exist only inside Zotero"
          + (f"; {copied:,} copied to {cfg.dump}" if copied else ""))
    if args.dry_run:
        print("Dry run: nothing was written.")
        return 0
    importer.save()
    scanner.scan(cfg, conn)
    if unmatched and not args.copy_pdfs:
        print("Run again with --copy-pdfs to copy the Zotero-only PDFs into the dump.")
    return 0


def cmd_setup_key(args: argparse.Namespace) -> int:
    """Store the Anthropic API key in the macOS Keychain and check that it works."""
    import getpass

    import anthropic

    from . import keys, llm

    key = getpass.getpass("Paste your Anthropic API key (it will not be shown): ").strip()
    if not key.startswith("sk-ant-"):
        print("That does not look like an Anthropic API key (it should start with sk-ant-).")
        return 1
    try:
        # A free request that only succeeds with a valid key.
        anthropic.Anthropic(api_key=key).models.retrieve(llm.MODEL)
    except anthropic.AuthenticationError:
        print("Anthropic rejected that key. Nothing was saved.")
        return 1
    except anthropic.APIConnectionError:
        print("Could not reach Anthropic to check the key. Nothing was saved; try again.")
        return 1
    keys.set_api_key(key)
    print("Key saved to the macOS Keychain (item \"refs\") and verified.")
    return 0


# --- Entry point -----------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="refs", description="A folder-aware reference manager on top of Dropbox.")
    parser.add_argument("--version", action="version", version=f"refs {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("scan", help="bring the index in line with Dropbox")
    p.add_argument("-v", "--verbose", action="store_true", help="list every change")
    p.add_argument("--json", action="store_true")
    p.set_defaults(run=cmd_scan)

    p = sub.add_parser("status", help="what the library holds")
    p.add_argument("--json", action="store_true")
    p.set_defaults(run=cmd_status)

    p = sub.add_parser("find", help="search, e.g. `refs find demand course:220a`")
    p.add_argument("query", nargs="*", help="words and key:value filters")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--all", action="store_true", help="no limit")
    p.add_argument("--json", action="store_true")
    p.set_defaults(run=cmd_find)

    p = sub.add_parser("bib", help="BibTeX for matching works, e.g. `refs bib blp` or `refs bib project:bets`")
    p.add_argument("query", nargs="+", help="words and key:value filters, as in `find`")
    p.add_argument("--limit", type=int, default=1, help="how many matches when searching by words")
    p.set_defaults(run=cmd_bib)

    p = sub.add_parser("contexts", help="courses and projects recognised from folders")
    p.add_argument("--json", action="store_true")
    p.set_defaults(run=cmd_contexts)

    p = sub.add_parser("dupes", help="files stored in more than one place")
    p.add_argument("--json", action="store_true")
    p.set_defaults(run=cmd_dupes)

    p = sub.add_parser("path", help="print the full path of the best match")
    p.add_argument("query", nargs="+")
    p.set_defaults(run=cmd_path)

    p = sub.add_parser("open", help="open the best match")
    p.add_argument("query", nargs="+")
    p.set_defaults(run=cmd_open)

    p = sub.add_parser("add", help="quick add: a URL, DOI, arXiv id, NBER number or PDF file")
    p.add_argument("thing")
    p.set_defaults(run=cmd_add)

    p = sub.add_parser("rename", help="propose canonical file names (add --apply to rename)")
    p.add_argument("query", nargs="*")
    p.add_argument("--apply", action="store_true", help="actually rename the files")
    p.add_argument("--undo", type=int, metavar="N", help="reverse the last N renames")
    p.set_defaults(run=cmd_rename)

    p = sub.add_parser("wanted", help="works saved without a PDF yet")
    p.set_defaults(run=cmd_wanted)

    p = sub.add_parser("process", help="read and look up the files a query selects (uses the API)")
    p.add_argument("query", nargs="*", help="words and key:value filters, as in `find`")
    p.add_argument("--all", action="store_true", help="every indexed file")
    p.add_argument("--limit", type=int, help="process at most this many files")
    p.add_argument("--redo", action="store_true", help="re-process files already done")
    p.add_argument("--relookup", action="store_true",
                   help="repeat only the registry lookup, reusing the saved reading (free)")
    p.add_argument("--workers", type=int, default=4)
    p.set_defaults(run=cmd_process)

    p = sub.add_parser("eval", help="check results against convention-style file names")
    p.add_argument("query", nargs="*")
    p.set_defaults(run=cmd_eval)

    p = sub.add_parser("zotero-import", help="import references from a Zotero library")
    p.add_argument("--zotero-dir", default="~/Zotero")
    p.add_argument("--dry-run", action="store_true", help="report what would happen, write nothing")
    p.add_argument("--copy-pdfs", action="store_true",
                   help="copy PDFs that exist only inside Zotero into the dump")
    p.set_defaults(run=cmd_zotero_import)

    p = sub.add_parser("serve", help="start the web interface")
    p.add_argument("--port", type=int, default=8377)
    p.add_argument("--open", action="store_true", help="open it in the browser")
    p.set_defaults(run=cmd_serve)

    p = sub.add_parser("setup-key", help="store the Anthropic API key in the Keychain")
    p.set_defaults(run=cmd_setup_key)

    args = parser.parse_args(argv)
    return args.run(args)


if __name__ == "__main__":
    sys.exit(main())
