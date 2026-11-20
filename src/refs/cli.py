"""The `refs` command.

    refs init                 set refs up (the first-run questions; the web page asks them too)
    refs doctor               check that everything is in order
    refs scan                 bring the index in line with Dropbox
    refs status               what the library holds
    refs find blp             search; add filters like course:101 kind:paper
    refs ask "IO paper on ice cream"   let Haiku rank the candidates (about $0.0005)
    refs contexts             the courses and projects the app has recognised
    refs collection           collections (courses, projects); add papers to them
    refs project new bets     a new project (a collection, optionally with a folder)
    refs dupes                files stored in more than one place
    refs open blp             open the best match in the default PDF viewer
    refs path blp             print its full path (handy inside scripts)
    refs watch                follow Dropbox for new PDFs (`refs serve` already does)

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
    """Separate free text from `key:value` filters. `#220a` names a collection;
    several (`#220a #bets`) must all hold."""
    terms, filters = [], {}
    for word in words:
        key, sep, value = word.partition(":")
        if len(word) > 1 and word.startswith("#"):
            name = word[1:].lower()
            filters["collection"] = f"{filters['collection']},{name}" if "collection" in filters else name
        elif sep and value and key.isalpha():
            filters[key.lower()] = value
        else:
            terms.append(word)
    return terms, filters


# --- Commands --------------------------------------------------------------------

def cmd_scan(args: argparse.Namespace) -> int:
    cfg = config.load()
    if problem := config.not_set_up(cfg):
        print(problem, file=sys.stderr)
        return 1
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
    if report.embedded:
        print(f"Semantic search: embedded {_plural(report.embedded, 'new or changed text')}.")
    if report.embed_error:
        print(f"Semantic search not updated ({report.embed_error}); keyword search is unaffected.",
              file=sys.stderr)
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    cfg = config.load()
    conn = index.connect(cfg)

    one = lambda sql: conn.execute(sql).fetchone()[0]   # noqa: E731  (tiny helper)
    data = {
        "dropbox_root": str(cfg.dropbox_root),
        "roots": cfg.roots,
        "dump": cfg.dump,                       # the quick-add folder
        "never_sent_to_ai": cfg.no_llm,         # confidential folders (no_llm)
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
    # Citation keys learned from project .bib files (bibkeys.py).
    from . import bibkeys, works
    all_works = works.load_all(cfg)
    data["bib_files"] = [bibkeys.label(cfg, p) for p in bibkeys.bib_paths(cfg)]
    data["works_with_project_keys"] = sum(1 for w in all_works.values() if w.get("bib_keys"))
    if args.json:
        print(json.dumps(data, indent=2))
        return 0

    if data["last_scan"] is None:
        print("The index is empty. Run `refs scan` first.")
        return 0
    print(f"Library at  {data['dropbox_root']}")
    print(f"Scanning    {', '.join(data['roots'])}")
    print(f"Quick-add   {data['dump']}")
    if data["never_sent_to_ai"]:
        print(f"Private     {', '.join(data['never_sent_to_ai'])} (never sent to an AI)")
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
    print(f"\nProject .bib files ({data['works_with_project_keys']:,} works have keys from them)")
    for name in data["bib_files"]:
        print(f"  {name}")
    return 0


def cmd_find(args: argparse.Namespace) -> int:
    from . import ask

    cfg = config.load()
    conn = index.connect(cfg)
    words, forced = ask.strip_force(args.query)
    terms, filters = _split_query(words)
    # Haiku is asked automatically only for a person at the terminal: --json
    # (scripts, coding agents) and --local never ask unless the query starts
    # with "?", and `refs ask` always does.
    mode = ("force" if forced or getattr(args, "force", False)
            else "off" if args.json or args.local else "auto")
    try:
        outcome = ask.search(cfg, conn, terms, filters, mode=mode)
    except ValueError as error:
        print(f"refs: {error}. Filters: #collection, course, project, term, area, in, topic, "
              "section, kind, year, author, status, tag, read, queued.", file=sys.stderr)
        return 2
    results = outcome.results if args.all else outcome.results[:args.limit]

    if args.json:
        # Absolute paths included so a script can open the file directly.
        for item in results:
            for loc in item["locations"]:
                loc["absolute_path"] = str(cfg.absolute(loc["path"]))
        print(json.dumps(results, indent=2, ensure_ascii=False))
        if outcome.message:
            print(f"refs: {outcome.message}", file=sys.stderr)
        return 0

    if not results:
        print(f"No matches{f' ({outcome.message})' if outcome.message else ''}.")
        return 1
    for item in results:
        label = item["kind"] + (f", {item['status']}" if item["status"] else ", not processed")
        if item.get("read_status") and item["read_status"] != "unread":
            label += f", {item['read_status']}"
        if item.get("queued"):
            label += ", queued"
        print(f"{_headline(item)}   [{label}]" + (f"  @{item['citekey']}" if item["citekey"] else ""))
        if item.get("why"):
            print(f"    ✦ {item['why']}")
        if item["venue"] or item["tags"]:
            print("    " + " · ".join(filter(None, [item["venue"], ", ".join(item["tags"][:6])])))
        for loc in item["locations"]:
            print(f"    {_where(loc)}")
            print(f"      {loc['path']}")
    print(f"\n{_plural(len(results), 'match')}" + ("" if args.all else f" (showing up to {args.limit})"))
    if outcome.message:
        print(f"✦ {outcome.message}")
    return 0


def cmd_ask(args: argparse.Namespace) -> int:
    """`refs ask ...` is `refs find ...` that always asks Haiku."""
    args.force, args.local = True, False
    return cmd_find(args)


def cmd_bib(args: argparse.Namespace) -> int:
    """Print BibTeX entries for the works a query selects."""
    from . import bib, librarybib, works

    cfg = config.load()
    conn = index.connect(cfg)
    if args.library:
        written = librarybib.write(cfg, conn)
        print(f"{librarybib.path(cfg)} {'written' if written else 'already up to date'}.")
        return 0
    if not args.query:
        print("Say which papers (`refs bib blp`, `refs bib project:bets`), or --library.", file=sys.stderr)
        return 2
    terms, filters = _split_query(args.query)
    all_works = works.load_all(cfg)
    # Only library works have BibTeX; with search words, keep the best `--limit`.
    results = [r for r in index.search(conn, terms, filters, limit=None, semantic=True)
               if r["work_id"] in all_works and "meta" in all_works[r["work_id"]]]
    if terms:
        results = results[:args.limit]
    # One entry per work, even when several of its files matched.
    chosen = list({r["work_id"]: all_works[r["work_id"]] for r in results}.values())
    if not chosen:
        print("No library works match.", file=sys.stderr)
        return 1
    if args.append is None:
        print("\n\n".join(bib.entry(w) for w in chosen))
        return 0

    # --append: add to a project's .bib, skipping papers it already holds
    # under any key (matched by key, DOI or title). With no file named, the
    # .bib of the collection the query names (`refs bib #bets --append`).
    from pathlib import Path

    from . import collection
    if args.append == "":
        names = [n for key in ("collection", "project", "course") if key in filters
                 for n in filters[key].lower().split(",")]
        registry = collection.load(cfg)
        target = next((path for n in names if (path := collection.bib_path(cfg, registry.get(n)))), None)
        if target is None:
            print("Say which .bib file (--append FILE.bib), or name a collection that has one "
                  "(`refs project new <name> --bib FILE.bib`).", file=sys.stderr)
            return 2
    else:
        target = Path(args.append).expanduser()
    if target.suffix != ".bib":
        print(f"{target} is not a .bib file.", file=sys.stderr)
        return 2
    added, present = bib.append(target, chosen)
    for work in added:
        print(f"added    @{work['citekey']}")
    for work, key in present:
        note = "" if key == work["citekey"] else f"  (refs calls it @{work['citekey']})"
        print(f"present  @{key}{note}")
    print(f"{len(added)} added to {target}, {len(present)} already there.")
    if added and "overleaf" in target.parts:
        print("This file is in an Overleaf repository: commit and push it there.")
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


def _print_collections(rows: list[dict]) -> None:
    """Courses by term (newest first), then projects."""
    from . import collection

    courses = sorted((r for r in rows if r["kind"] == "course"),
                     key=lambda r: (-collection._term_rank(r["term"] or ""), r["name"]))
    projects = [r for r in rows if r["kind"] != "course"]
    for title, group in (("courses", courses), ("projects", projects)):
        if not group:
            continue
        print(title)
        for r in group:
            notes = [r["term"] or "", "shared" if r["shared"] else "",
                     "from folders" if r["implicit"] else "",
                     f"folder {r['folder']}" if r["folder"] else "",
                     f"bib {r['bib_file']}" if r["bib_file"] else ""]
            depth = r["name"].count("/")
            print(f"  {'  ' * depth}{r['name']:<{28 - 2 * depth}} {r['works']:>5}  "
                  + " · ".join(n for n in notes if n))


def cmd_collection(args: argparse.Namespace) -> int:
    """`refs collection`: list collections. `add`/`remove` a paper (the best
    match of a search, or every match with --all), `rename`, `new` or `delete` one."""
    from . import collection, works

    cfg = config.load()
    if args.action == "new":
        return _new_collection(cfg, args, kind=args.kind)
    conn = index.connect(cfg)
    if args.action == "list":
        rows = collection.listing(cfg, conn)
        if args.json:
            print(json.dumps(rows, indent=2, ensure_ascii=False))
        elif rows:
            _print_collections(rows)
        else:
            print("No collections yet. `refs project new <name>` makes one; "
                  "`refs collection add <name> <search>` puts a paper in one.")
        return 0
    try:
        if args.action == "delete":
            if len(args.words) != 1:
                print("Usage: refs collection delete NAME", file=sys.stderr)
                return 2
            count = collection.delete(cfg, args.words[0])
            index.refresh_derived(cfg, conn)
            conn.commit()
            print(f"Deleted {collection.clean_name(args.words[0])} (taken out of {_plural(count, 'paper')}). "
                  "Papers in it only because of their folder stay as they are.")
            return 0
        if args.action == "rename":
            if len(args.words) != 2:
                print("Usage: refs collection rename OLD NEW", file=sys.stderr)
                return 2
            count = collection.rename(cfg, *args.words)
            index.refresh_derived(cfg, conn)
            conn.commit()
            print(f"Renamed {args.words[0]} to {collection.clean_name(args.words[1])} "
                  f"({_plural(count, 'paper')}). Papers in it only because of their folder stay as they are.")
            return 0
        # add / remove NAME SEARCH...
        if len(args.words) < 2:
            print(f"Usage: refs collection {args.action} NAME SEARCH...", file=sys.stderr)
            return 2
        name = collection.clean_name(args.words[0])
    except collection.Invalid as error:
        print(f"refs: {error}.", file=sys.stderr)
        return 2
    all_works = works.load_all(cfg)
    terms, filters = _split_query(args.words[1:])
    found = [r for r in index.search(conn, terms, filters, limit=None, semantic=bool(terms))
             if r["work_id"] in all_works and "meta" in all_works[r["work_id"]]]
    if not args.all:
        found = found[:1]
    if not found:
        print("No matching paper.", file=sys.stderr)
        return 1
    adding = args.action == "add"
    changed = set(collection.change(cfg, [r["work_id"] for r in found], name, add=adding))
    all_works = works.load_all(cfg)
    for item in found:
        index.update_collections(conn, all_works[item["work_id"]])
        state = ("added to " if adding else "taken out of ") if item["work_id"] in changed \
            else ("already in " if adding else "not in ")
        print(f"{state}{name}: {_headline(item)}  @{item['citekey']}")
    if not adding and any(name in r["collections"] and r["work_id"] not in changed for r in found):
        print("(A paper in it because of its folder stays in it until the file moves.)")
    return 0


def _new_collection(cfg, args: argparse.Namespace, kind: str) -> int:
    """`refs project new` / `refs collection new`: register a collection, and
    with --make-folder create its folder."""
    from . import collection, contexts, organize

    try:
        name = collection.clean_name(args.name)
    except collection.Invalid as error:
        print(f"refs: {error}.", file=sys.stderr)
        return 2
    folder = args.folder
    if args.make_folder and not folder:
        if cfg.layout == "phd" and kind == "project":
            folder = f"phd/research/{name}/readings"
        else:
            print("Say which folder to make with --folder (relative to the library folder).",
                  file=sys.stderr)
            return 2
    if args.shared and not folder:
        print("A shared collection needs its shared folder: --folder phd/research/<name>/readings.",
              file=sys.stderr)
        return 2
    try:
        existed = name in collection.load(cfg)
        if args.term and not contexts.term_of(args.term):
            raise collection.Invalid(f"{args.term!r} is not a term (say 2026-fall)")
        if args.make_folder:
            # Inside the watched folders, never a managed one; made first, so a
            # refused folder saves nothing.
            folder = organize.make_folder(cfg, folder)
        entry = collection.register(cfg, name, kind=kind, term=args.term, folder=folder,
                                    shared=True if args.shared else None, bib_file=args.bib)
    except (collection.Invalid, organize.Refused) as error:
        print(f"refs: {error}.", file=sys.stderr)
        return 2
    print(f"{'Updated' if existed else 'New'} {entry['kind']} {name}"
          + (f" ({entry['term']})" if entry["term"] else ""))
    if entry["folder"]:
        made = " (made)" if args.make_folder else ""
        print(f"  folder  {entry['folder']}{made}" + ("  · shared with coauthors" if entry["shared"] else ""))
        if not any(entry["folder"] == r or entry["folder"].startswith(r.rstrip("/") + "/") for r in cfg.roots):
            print("  (refs does not watch this folder: it is outside the watched folders)")
    if entry["bib_file"]:
        print(f"  bib     {entry['bib_file']}  (`refs bib collection:{name} --append` adds to it)")
    print(f"Add papers with `refs collection add {name} <search>`, or on the page.")
    return 0


def cmd_project(args: argparse.Namespace) -> int:
    """`refs project new <name>`: the plan's name for making a collection."""
    cfg = config.load()
    return _new_collection(cfg, args, kind="course" if args.course else "project")


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
    results = index.search(conn, terms, filters, limit=1, semantic=True)
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

    from . import llm, pipeline, resolve

    cfg = config.load()
    conn = index.connect(cfg)
    terms, filters = _split_query(args.query)
    if not terms and not filters and not args.all:
        print("Say which files (e.g. `refs process course:101 kind:paper`), or pass --all.",
              file=sys.stderr)
        return 2
    # Works without a PDF have nothing to read.
    files = [f for f in index.search(conn, terms, filters, limit=None) if f["locations"]]
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
        # A lookup that was skipped because OpenAlex's quota was used up is
        # retried once OpenAlex is available again (the saved reading is reused).
        waiting = old.get("pending") and not resolve.blocked_for("api.openalex.org")
        return bool(waiting) or any("failed" in issue for issue in old["issues"])

    todo = [f for f in files if needs_work(f)]
    if args.limit:
        todo = todo[:args.limit]
    print(f"{_plural(len(files), 'file')} selected, {len(todo):,} to process.")
    if not todo:
        return 0
    # A large run says what it costs first (budget.py); --yes skips the question.
    from . import budget
    if len(todo) > budget.BULK and not args.yes and not args.relookup:
        question = f"Reading {len(todo):,} PDFs costs about ${budget.estimate(len(todo)):.2f}. Go ahead? [y/N] "
        if not sys.stdin.isatty():
            print(f"{question.split(' Go')[0]} Run again with --yes to go ahead.", file=sys.stderr)
            return 1
        if not input(question).strip().lower().startswith("y"):
            print("Nothing was read.")
            return 1

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
                budget.record(conn, llm.cost(read["usage"]))     # counts toward the month
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

    from . import capture, ingest, resolve, works

    cfg = config.load()
    conn = index.connect(cfg)
    dump = cfg.absolute(cfg.dump)
    thing, source_url, target = args.thing, args.source_url, None
    # What the browser read off the page (the bookmarklet), if anything: the
    # page's DOI, which also helps identify a PDF that does not print one.
    hint_ids = capture.hinted(capture.Target(), doi=args.doi).ids

    # 1. Get a PDF into the dump: copy a local file, or download an open copy.
    local = Path(thing).expanduser()
    if local.is_file() and local.suffix.lower() == ".pdf":
        path = dump / local.name
        if local.resolve().is_relative_to(cfg.dropbox_root):
            path = local.resolve()                       # already in Dropbox: use it where it is
        elif not path.exists():
            shutil.copy2(local, path)
    else:
        # Each candidate link in turn (the page's PDF link, arXiv, NBER), then
        # an open-access copy elsewhere if those are paywalled.
        target = capture.hinted(capture.parse(thing), doi=args.doi, pdf_url=args.pdf_url)
        target = capture.discover(target, fetch_page=not args.no_page)
        source_url = source_url or target.page_url or target.pdf_url
        path = capture.download_first(target.pdf_urls, dump)
        if path is None and (doi := target.ids.get("doi")):
            path = capture.download_first(capture.open_copies(doi), dump)
        hint_ids = {**target.ids, **hint_ids}

    # 2. No PDF could be fetched: save the paper itself, to be completed later.
    if path is None:
        record, _ = resolve.by_ids(target.ids) if target.ids else (None, None)
        if record is None:
            print("Could not identify a paper or find an open PDF from that. "
                  f"Download the PDF yourself and save it into {cfg.dump}.")
            return 1
        work = works.create_stub(cfg, record.to_dict(), source_url)
        if args.queue:
            _queue(cfg, work)
        print(f"Saved without a PDF (no open copy found): {record.title} ({record.year})"
              + ("  [queued]" if args.queue else ""))
        print(f"  @{work['citekey']} . Save the PDF into {cfg.dump} and it will attach itself.")
        return 0

    # 3. Index the new file, then read it, file it under its work and, inside
    #    the dump, rename it (ingest.py; the folder watcher runs the same steps).
    #    The lock keeps the watcher from reading the same new file in parallel.
    rel = str(path.relative_to(cfg.dropbox_root))
    with ingest.exclusive(cfg):
        scanner.scan(cfg, conn)
        row = conn.execute("SELECT hash FROM paths WHERE path=?", (rel,)).fetchone()
        if row is None:
            print(f"The file was saved to {rel} but could not be indexed.")
            return 1
        [item] = ingest.ingest(cfg, conn, [(rel, row["hash"])], source_url=source_url,
                               check_complete=False, hint_ids=hint_ids)
    if item.read is None:
        print(f"Saved to {item.rel}, but not read: {item.skipped}.")
        return 1

    if args.queue and item.work:
        _queue(cfg, item.work)
    print(f"Added {ingest.headline(item)}" + ("  [queued]" if args.queue and item.work else ""))
    print(f"  {item.rel}" + (f"   @{item.work['citekey']}" if item.work else ""))
    for issue in item.read["issues"]:
        print(f"  ! {issue}")
    return 0


def _queue(cfg, work: dict, on: bool = True) -> None:
    """Put a work on the reading queue (or take it off), in its file and the index."""
    from . import reading, records, works

    reading.set_queued(work, on)
    records.write_json(works.works_dir(cfg) / f"{work['id']}.json", work)
    conn = index.connect(cfg)
    conn.execute("UPDATE files SET queued=? WHERE work_id=?", (work["user"].get("queued"), work["id"]))
    conn.commit()


def cmd_queue(args: argparse.Namespace) -> int:
    """`refs queue`: list the reading queue. `refs queue blp`: queue the best
    match. `refs queue --remove blp`: take it off."""
    from . import reading, works

    cfg = config.load()
    conn = index.connect(cfg)
    all_works = works.load_all(cfg)
    if args.query:
        terms, filters = _split_query(args.query)
        found = [r for r in index.search(conn, terms, filters, limit=None, semantic=True)
                 if r["work_id"] in all_works and "meta" in all_works[r["work_id"]]]
        if not found:
            print("No matching paper.", file=sys.stderr)
            return 1
        work = all_works[found[0]["work_id"]]
        _queue(cfg, work, on=not args.remove)
        print(("Off the queue: " if args.remove else "Queued: ") + _headline(found[0])
              + f"  @{work['citekey']}")
        return 0

    queued = sorted((w for w in all_works.values() if w["user"].get("queued") and "meta" in w),
                    key=lambda w: w["user"]["queued"])
    if args.json:
        print(json.dumps([{"work_id": w["id"], "citekey": w["citekey"], "title": w["meta"].get("title"),
                           "year": w["meta"].get("year"), "queued": w["user"]["queued"],
                           "read_status": reading.effective(w)[0]} for w in queued],
                         indent=2, ensure_ascii=False))
        return 0
    if not queued:
        print("The queue is empty. Add a paper with `refs queue <search>` or `refs add <link> --queue`.")
        return 0
    for n, w in enumerate(queued, 1):
        meta = w["meta"]
        who = ", ".join(a.get("family", "") for a in (meta.get("authors") or [])[:3])
        status = reading.effective(w)[0]
        print(f"{n:>3}. {who} ({meta.get('year') or 'n.d.'}) {meta.get('title') or ''}"
              f"{f'   [{status}]' if status != 'unread' else ''}   @{w['citekey']}")
        print(f"     queued {w['user']['queued'][:10]}" + ("" if w["files"] else " · no pdf yet"))
    return 0


def cmd_rename(args: argparse.Namespace) -> int:
    """Propose (default) or apply canonical file names for the files a query selects."""
    from . import rename, works

    cfg = config.load()
    conn = index.connect(cfg)
    if args.undo:
        for original, was, action in rename.undo(cfg, args.undo):
            if action == "copy":
                print(f"trashed copy  {was}  (original stays at {original})")
            else:
                print(f"restored  {was}  ->  {original}")
        scanner.scan(cfg, conn)
        return 0

    terms, filters = _split_query(args.query)
    if not terms and not filters:
        print("Say which files, e.g. `refs rename in:dump`.", file=sys.stderr)
        return 2
    all_works = works.load_all(cfg)
    items = index.search(conn, terms, filters, limit=None)
    proposals, copies, protected = rename.plan(cfg, conn, items, all_works)

    if args.apply:
        done = rename.apply_many(cfg, proposals)
        scanner.scan(cfg, conn)
        print(f"Renamed {len(done):,} of {len(proposals):,} files. "
              f"Undo with `refs rename --undo {len(done)}`.")
    else:
        for proposal in proposals:
            print(f"{proposal.path}\n    -> {proposal.new_name}")
        print(f"\n{_plural(len(proposals), 'file')} would be renamed"
              f"{f', {len(protected)} protected' if protected else ''}. "
              "Nothing was changed; add --apply to rename.")
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

    # Not set up yet: still serve (the page works, just empty), but say why.
    if problem := config.not_set_up(config.load()):
        print(problem, file=sys.stderr, flush=True)
    url = f"http://refs.localhost:{args.port}"
    print(f"refs is running at {url}  (Ctrl-C to stop)", flush=True)
    if args.open:
        subprocess.Popen(["open", url])
    # Always 127.0.0.1: the server is reachable from this Mac only, never from
    # the network. (That is also why the address keeps its port number: without
    # admin rights, macOS only allows port 80 on all network interfaces.)
    uvicorn.run(create_app(watch=not args.no_watch), host="127.0.0.1", port=args.port,
                log_level="warning")
    return 0


def cmd_watch(args: argparse.Namespace) -> int:
    """Follow Dropbox in the foreground, without the web interface (watch.py)."""
    import time

    from .watch import Watcher, notify

    cfg = config.load()
    if problem := config.not_set_up(cfg):
        print(problem, file=sys.stderr)
        return 1
    watcher = Watcher(cfg, notifier=None if args.quiet else notify)
    if args.once:
        watcher.run_pass()
        return 0
    if not watcher.start():
        return 1
    print("Watching for changes (Ctrl-C to stop).", flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        watcher.stop()
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
            file_hash, on_disk, indexed = zotero.locate_attachment(
                cfg, zotero_dir, attachment, known_paths, known_hashes)
            if indexed:
                hashes.append(file_hash)
                matched += 1
            elif on_disk is not None:
                unmatched += 1
                # A PDF that lives only inside Zotero: optionally copy it to the
                # dump. Its content hash is attached to the work right away, so
                # the copy belongs to the right paper without being re-identified.
                if args.copy_pdfs and not args.dry_run:
                    target = cfg.absolute(cfg.dump) / on_disk.name
                    if not target.exists():
                        shutil.copy2(on_disk, target)
                        copied += 1
                    hashes.append(file_hash)
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


def cmd_service(args: argparse.Namespace) -> int:
    """Install, remove or check the background service that runs refs at login."""
    from . import service

    actions = {"install": service.install, "uninstall": service.uninstall,
               "restart": service.restart, "status": service.status}
    print(actions[args.action]())
    # Not set up yet: the page's first-run screens ask the questions.
    if args.action == "install" and service.is_loaded() and config.not_set_up(config.load()):
        url = f"http://refs.localhost:{service.PORT}"
        print(f"Opening {url} to set refs up (or run `refs init` here).")
        subprocess.Popen(["open", url])
    return 0


def cmd_init(args: argparse.Namespace) -> int:
    """Set refs up in the terminal: the questions of the first-run page."""
    import getpass
    from pathlib import Path

    from . import keys, onboarding, service, settings

    cfg = config.load()
    if config.not_set_up(cfg) is None and not args.again:
        print(f"refs is already set up (watching {', '.join(cfg.roots)}). Change settings on the "
              "settings page (key `,`), or start over with `refs init --again`.")
        return 0

    def ask(prompt: str, default: str = "") -> str:
        shown = f" [{default}]" if default else ""
        return input(f"{prompt}{shown}: ").strip() or default

    def pick(answer: str, options: list[str]) -> list[str]:
        """Numbers from a printed list, or the texts themselves."""
        chosen = []
        for token in answer.replace(",", " ").split():
            chosen.append(options[int(token) - 1] if token.isdigit() and 0 < int(token) <= len(options)
                          else token.strip("/"))
        return chosen

    print("Set up refs. Enter takes the [default]; Ctrl-C stops without saving anything.\n")
    try:
        # 1. The library folder.
        libraries = onboarding.library_candidates()
        if cfg.dropbox_root != Path.home().resolve() and all(l["path"] != str(cfg.dropbox_root) for l in libraries):
            libraries.insert(0, {"label": "set now", "path": str(cfg.dropbox_root)})
        print("Where do you keep your papers? (the library folder)")
        for i, lib in enumerate(libraries, 1):
            print(f"  {i}. {lib['label']:<13} {lib['path']}")
        library = pick(ask("Number, or a path", "1"), [lib["path"] for lib in libraries])[0]
        library = str(Path(library).expanduser().resolve())

        # 2. The folders with papers.
        print("\nCounting PDFs …")
        found = onboarding.survey(Path(library))
        top = sorted((c for c in found["tree"]["children"] if c["pdfs"]), key=lambda c: -c["pdfs"])
        for i, c in enumerate(top, 1):
            print(f"  {i:>2}. {c['pdfs']:>7,}  {c['name']}")
        roots: list[str] = []
        while not roots:
            roots = pick(ask("Which hold papers? Numbers like `1 3`, or paths inside the library"),
                         [c["path"] for c in top])

        # 3. The quick-add folder.
        print("\nPapers you add in refs are saved in the quick-add folder (created if new).")
        dump = ask("Quick-add folder", cfg.dump).strip("/")

        # 4. Private folders: suggestions from folder names, then any others.
        exclude: list[str] = []
        no_llm: list[str] = []
        offered = [s for s in found["suggestions"] if onboarding._under(s["path"], roots)]
        if offered:
            print("\nFolders whose names suggest they are private:")
            for i, s in enumerate(offered, 1):
                what = "never send to Claude" if s["kind"] == "no_llm" else "leave out entirely"
                print(f"  {i}. [{'x' if s['ticked'] else ' '}] {s['path']}  ({what})")
            ticked = " ".join(str(i) for i, s in enumerate(offered, 1) if s["ticked"])
            answer = ask("Which to use (numbers; `none` for none)", ticked or "none")
            for path in pick("" if answer == "none" else answer, [s["path"] for s in offered]):
                kind = next((s["kind"] for s in offered if s["path"] == path), "exclude")
                (no_llm if kind == "no_llm" else exclude).append(path)
        exclude += pick(ask("Other folders to leave out entirely (paths, or Enter)"), [])
        no_llm += pick(ask("Other folders to keep from Claude (paths, or Enter)"), [])

        # 5. Reading notes in Obsidian.
        vaults = settings.obsidian_vaults()
        print("\nReading notes can be Markdown files in an Obsidian vault.")
        for i, vault in enumerate(vaults, 1):
            print(f"  {i}. {vault}")
        answer = ask("Vault (number or path; `none` for no notes)", "1" if vaults else "none")
        vault = "" if answer == "none" else pick(answer, vaults)[0]

        # 6. The API key and what to read.
        print("\nTo tell what each PDF is, refs sends the text of its first three pages (an image of")
        print("page one for scans) to Claude Haiku, then checks Crossref and OpenAlex. Never file")
        print("names, notes or private folders. About $0.001 a PDF, on your own Anthropic API key.")
        if settings.api_key_status() == "missing":
            for _ in range(2):
                key = getpass.getpass("Anthropic API key (hidden; Enter to skip for now): ").strip()
                if not key:
                    break
                if problem := keys.problem_with(key):
                    print(f"  {problem}.")
                    continue
                keys.set_api_key(key)
                print("  Saved in the macOS Keychain.")
                break
        n = onboarding.pdfs_in(found["tree"], roots, exclude)
        cost = onboarding.estimate(n)
        answer = ask(f"Read [a]ll {n:,} PDFs now (about ${cost['dollars']:.2f}), only papers [d]umped "
                     "from now on, or [n]othing", "d").lower()[:1]
        auto = {"a": "all", "n": "none"}.get(answer, "dump")
    except (KeyboardInterrupt, EOFError):
        print("\nStopped; nothing was saved.")
        return 1

    changes = {"dropbox_root": library, "roots": roots, "dump": dump, "exclude_paths": exclude,
               "no_llm": no_llm, "obsidian_vault": vault, "auto_process": auto,
               "contexts": onboarding.rules_for(found, roots)}
    try:
        saved = settings.save(changes)
    except settings.SettingError as error:
        print(f"Not saved: {error}. Run `refs init` again.")
        return 1
    print(f"\nSaved {config.config_path()}.")
    for rule in saved.contexts:
        print(f"  Courses: {rule} (term folders found)")

    # 7. Running in the background: a running refs must restart to see the new settings.
    # The login service reads the default settings file only; with another one
    # (REFS_CONFIG), it is left alone.
    import os
    if os.environ.get("REFS_CONFIG"):
        print("These settings are not the default file, so the background service was left alone.")
    elif service.is_loaded():
        print(service.restart(), "refs is following your folders at", f"http://refs.localhost:{service.PORT}")
    elif ask("\nStart refs in the background now and at login? [Y/n]", "y").lower().startswith("y"):
        print(service.install())
    else:
        print("Start it with `refs serve --open`, or index now with `refs scan`.")
    if auto == "all":
        print("Reading happens in the background; the page shows each paper as it is read.")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    """Check settings, library, index, key, spending, notes, model and service."""
    from . import doctor

    found = doctor.checks(online=not args.offline)
    for check in found:
        print(f"  {check.state} {check.name:<13} {check.detail}")
        if check.fix:
            print(f"    {'':<13} → {check.fix}")
    bad = sum(c.state == doctor.BAD for c in found)
    print("\nAll in order." if not bad else f"\n{bad} problem{'s' if bad > 1 else ''} above.")
    return 1 if bad else 0


def cmd_uninstall(args: argparse.Namespace) -> int:
    """Take refs off this Mac: service, index, caches, log, settings, key."""
    from . import doctor

    cfg = config.load()
    items = doctor.leftovers(cfg, args.keep_settings, args.keep_key)
    print("This removes from this Mac:")
    for what, path in items:
        print(f"  - {what}" + (f"  ({doctor._home(path)})" if path else ""))
    print("It keeps your PDFs, your notes, and refs' records in "
          f"{doctor._home(cfg.absolute(cfg.state_dir))} (a reinstall picks them up again;")
    print("delete that folder yourself if you will not).")
    if not items:
        print("Nothing to remove.")
    elif not args.yes:
        if not sys.stdin.isatty():
            print("Run again with --yes to go ahead.", file=sys.stderr)
            return 1
        if not input("Go ahead? [y/N] ").strip().lower().startswith("y"):
            print("Nothing was removed.")
            return 1
    for line in doctor.uninstall(cfg, args.keep_settings, args.keep_key):
        print(f"  removed {line}")
    print("Last step, to remove the program itself: uv tool uninstall refs")
    return 0


def cmd_skill(args: argparse.Namespace) -> int:
    """Install the agent skill, or say where it is installed."""
    from . import agents

    if args.action == "install":
        for line in agents.install():
            print(line)
        print("Agents now know to look papers up with refs (start a new agent session).")
    else:
        for name, dest, state in agents.status():
            print(f"{name:<13} {state:<8} {dest}")
    return 0


def cmd_setup_key(args: argparse.Namespace) -> int:
    """Store the Anthropic API key in the macOS Keychain and check that it works."""
    import getpass

    from . import keys

    key = getpass.getpass("Paste your Anthropic API key (it will not be shown): ").strip()
    if problem := keys.problem_with(key):
        print(f"{problem[0].upper()}{problem[1:]}. Nothing was saved.")
        return 1
    keys.set_api_key(key)
    print("Key saved to the macOS Keychain (item \"refs\") and verified.")
    return 0


# --- Entry point -----------------------------------------------------------------

def _collection_options(p: argparse.ArgumentParser) -> None:
    p.add_argument("--term", help="a course's term, e.g. 2026-fall")
    p.add_argument("--folder", help="its folder, relative to the library folder")
    p.add_argument("--make-folder", action="store_true",
                   help="create the folder (default for a project: phd/research/<name>/readings)")
    p.add_argument("--shared", action="store_true", help="the folder is shared with coauthors")
    p.add_argument("--bib", help="the project's .bib file (~/... on this Mac, or inside Dropbox)")


def _collection_entry(args: argparse.Namespace) -> int:
    """`refs collection new NAME` takes the name from the words."""
    if args.action == "new":
        if len(args.words) != 1:
            print("Usage: refs collection new NAME [--kind course|project] [--folder ...]", file=sys.stderr)
            return 2
        args.name = args.words[0]
    return cmd_collection(args)


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

    p = sub.add_parser("find", help="search, e.g. `refs find demand course:101`")
    p.add_argument("query", nargs="*", help="words and key:value filters")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--all", action="store_true", help="no limit")
    p.add_argument("--json", action="store_true")
    p.add_argument("--local", action="store_true",
                   help="never ask Haiku (it is asked only when a query reads like a "
                        "description and the local results look unsure)")
    p.set_defaults(run=cmd_find)

    p = sub.add_parser("ask", help='let Haiku rank the candidates, e.g. `refs ask IO paper on ice cream`')
    p.add_argument("query", nargs="+", help="a description, plus key:value filters if you like")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--all", action="store_true", help="no limit")
    p.add_argument("--json", action="store_true")
    p.set_defaults(run=cmd_ask)

    p = sub.add_parser("bib", help="BibTeX for matching works, e.g. `refs bib blp` or `refs bib project:thesis`")
    p.add_argument("query", nargs="*", help="words and key:value filters, as in `find`")
    p.add_argument("--limit", type=int, default=1, help="how many matches when searching by words")
    p.add_argument("--library", action="store_true",
                   help="write library.bib (every paper, with its PDF and collections) now")
    p.add_argument("--append", metavar="FILE.bib", nargs="?", const="",
                   help="add the entries to this .bib file instead of printing them "
                        "(papers already in it, under any key, are skipped)")
    p.set_defaults(run=cmd_bib)

    p = sub.add_parser("contexts", help="courses and projects recognised from folders")
    p.add_argument("--json", action="store_true")
    p.set_defaults(run=cmd_contexts)

    p = sub.add_parser("collection",
                       help="collections (courses, projects): list, add, remove, rename, new, delete")
    p.add_argument("action", nargs="?", default="list",
                   choices=["list", "add", "remove", "rename", "new", "delete"])
    p.add_argument("words", nargs="*",
                   help="add/remove: NAME SEARCH...; rename: OLD NEW; new/delete: NAME")
    p.add_argument("--all", action="store_true", help="add/remove every match, not only the best")
    p.add_argument("--kind", choices=["course", "project"], help="new: course or project")
    _collection_options(p)
    p.add_argument("--json", action="store_true")
    p.set_defaults(run=_collection_entry)

    p = sub.add_parser("project", help="`refs project new <name>`: a new project or course")
    p.add_argument("action", choices=["new"])
    p.add_argument("name")
    p.add_argument("--course", action="store_true", help="a course, not a project")
    _collection_options(p)
    p.set_defaults(run=cmd_project)

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
    p.add_argument("--queue", action="store_true", help="also put it on the reading queue")
    # Passed by the page when the bookmarklet has read the paper's page already.
    p.add_argument("--doi", help="the paper's DOI, as its page gives it")
    p.add_argument("--pdf-url", help="a link to the PDF, tried first")
    p.add_argument("--source-url", help="the page the paper came from (kept with the work)")
    p.add_argument("--no-page", action="store_true", help="do not fetch the page itself")
    p.set_defaults(run=cmd_add)

    p = sub.add_parser("queue", help="the reading queue: list it, or `refs queue blp` to queue a paper")
    p.add_argument("query", nargs="*", help="words and key:value filters; the best match is queued")
    p.add_argument("--remove", action="store_true", help="take the match off the queue instead")
    p.add_argument("--json", action="store_true")
    p.set_defaults(run=cmd_queue)

    p = sub.add_parser("rename", help="propose canonical file names (add --apply to rename)")
    p.add_argument("query", nargs="*")
    p.add_argument("--apply", action="store_true", help="actually rename the files")
    p.add_argument("--undo", type=int, metavar="N", help="reverse the last N renames, moves or copies")
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
    p.add_argument("--yes", action="store_true", help="do not ask before reading many PDFs")
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
    p.add_argument("--no-watch", action="store_true",
                   help="do not follow Dropbox for new and changed PDFs")
    p.set_defaults(run=cmd_serve)

    p = sub.add_parser("watch", help="follow Dropbox for new PDFs (already part of `serve`)")
    p.add_argument("--once", action="store_true", help="one scan-and-read pass, then stop")
    p.add_argument("--quiet", action="store_true", help="no macOS notifications")
    p.set_defaults(run=cmd_watch)

    p = sub.add_parser("service", help="run refs in the background, starting at login")
    p.add_argument("action", choices=["install", "uninstall", "restart", "status"])
    p.set_defaults(run=cmd_service)

    p = sub.add_parser("setup-key", help="store the Anthropic API key in the Keychain")
    p.set_defaults(run=cmd_setup_key)

    p = sub.add_parser("doctor", help="check that everything is in order (send this when asking for help)")
    p.add_argument("--offline", action="store_true", help="skip the checks that use the network")
    p.set_defaults(run=cmd_doctor)

    p = sub.add_parser("uninstall", help="remove refs' service, index, caches, settings and key from this Mac")
    p.add_argument("--keep-settings", action="store_true", help="keep ~/.config/refs/config.toml")
    p.add_argument("--keep-key", action="store_true", help="keep the API key in the Keychain")
    p.add_argument("--yes", action="store_true", help="do not ask before removing")
    p.set_defaults(run=cmd_uninstall)

    p = sub.add_parser("skill", help="teach coding agents (Claude Code, Codex) to use refs")
    p.add_argument("action", choices=["install", "status"], nargs="?", default="status")
    p.set_defaults(run=cmd_skill)

    p = sub.add_parser("init", help="set refs up: the first-run questions, in the terminal")
    p.add_argument("--again", action="store_true", help="ask again although refs is set up")
    p.set_defaults(run=cmd_init)

    args = parser.parse_args(argv)
    return args.run(args)


if __name__ == "__main__":
    sys.exit(main())
