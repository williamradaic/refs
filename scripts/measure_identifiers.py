"""How far do identifiers alone get us? (A measurement, not part of the app.)

For every file the index currently calls a paper (or cannot classify), read the
opening pages, look for an identifier, and ask the registry. Where the file name
already follows the house convention, compare the registry's answer against it:
the name is independent ground truth for surname and year.

Run:  uv run python scripts/measure_identifiers.py
"""

import collections
import json
import sys

from refs import config, extract, index, naming, resolve

cfg = config.load()
conn = index.connect(cfg)
files = [f for f in index.search(conn, [], {}, limit=None) if f["kind"] in {"paper", "unknown"}]

stats = collections.Counter()
mismatches, unresolved_ids, rows = [], [], []

for n, f in enumerate(files, 1):
    rel = f["locations"][0]["path"]
    if cfg.absolute(rel).as_posix().startswith(tuple(str(cfg.absolute(p)) for p in cfg.no_llm)):
        stats["skipped (confidential folder)"] += 1
        continue
    info = extract.read_pdf(cfg.absolute(rel))
    stats["files"] += 1
    if info.error:
        stats["could not open"] += 1
        continue
    if not info.has_text:
        stats["no text layer (scan)"] += 1
    ids = {**f["ids"], **info.ids}          # file-name ids plus first-page ids
    if not ids:
        stats["no identifier"] += 1
        continue
    stats["has identifier"] += 1
    try:
        record, via = resolve.by_ids(ids)
    except Exception as error:               # network trouble: count and move on
        stats[f"lookup failed ({type(error).__name__})"] += 1
        continue
    if record is None:
        stats["identifier not in registry"] += 1
        unresolved_ids.append((rel, ids))
        continue
    stats["resolved"] += 1
    stats[f"  via {via}"] += 1
    rows.append({"path": rel, "via": via, "record": record.to_dict()})

    # Ground-truth check for files already named by the convention.
    guess = naming.parse_filename(rel.rsplit("/", 1)[-1])
    if guess.style == "convention" and guess.authors and guess.authors[-2:] != ["et", "al"]:
        stats["checkable against file name"] += 1
        registry = {naming.slugify(s) for s in record.surnames}
        named = set(guess.authors)
        # Multi-word surnames ("scott-morton") are split in file names, so
        # compare on the pieces.
        registry_pieces = {piece for s in registry for piece in s.split("-")}
        surname_ok = bool(named & registry_pieces)
        year_ok = record.year is not None and abs(record.year - guess.year) <= 2
        if surname_ok and year_ok:
            stats["  agrees with file name"] += 1
        else:
            stats["  DISAGREES with file name"] += 1
            mismatches.append((rel, via, ids, record.title, record.surnames, record.year))
    if n % 40 == 0:
        print(f"  {n}/{len(files)}", file=sys.stderr)

print(f"\nFiles examined: {len(files)} (kind paper or unknown)\n")
for key, value in stats.items():
    print(f"{value:>5}  {key}")
print("\nDisagreements with the file name:")
for rel, via, ids, title, surnames, year in mismatches:
    print(f"  {rel.rsplit('/', 1)[-1]}\n     via {via} {ids}\n     registry says: {surnames} ({year}) {title!r}")
print("\nIdentifier found but unknown to the registry (first 10):")
for rel, ids in unresolved_ids[:10]:
    print(f"  {rel.rsplit('/', 1)[-1]}  {ids}")

out = cfg.index_db.parent / "measure-identifiers.json"
out.write_text(json.dumps(rows, indent=1, ensure_ascii=False))
print(f"\nFull results: {out}")
