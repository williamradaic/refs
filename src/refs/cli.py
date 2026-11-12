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
    print("\nBy kind (provisional: guessed from names and folders)")
    for kind, count in data["by_kind"].items():
        print(f"  {count:>6,}  {kind}")
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
              "section, kind, year, author.", file=sys.stderr)
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
        print(f"{_headline(item)}   [{item['kind']}]")
        for loc in item["locations"]:
            print(f"    {_where(loc)}")
            print(f"      {loc['path']}")
    print(f"\n{_plural(len(results), 'match')}" + ("" if args.all else f" (showing up to {args.limit})"))
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

    args = parser.parse_args(argv)
    return args.run(args)


if __name__ == "__main__":
    sys.exit(main())
