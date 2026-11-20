# refs, research first: design and implementation plan

Synthesis of a design discussion (November 2026). Read with `docs/development.md`
(map of the code, how files are identified, where state lives).

## Goal

Make refs a research-first reference manager. Every reading (research
literature, plus course readings worth keeping) lives once in a central library
folder. Courses and projects are collections (tags) in refs' records, not
folders. Course material (slides, problem sets, syllabi) leaves refs entirely:
the course folders in Dropbox already organise it. The structure must survive
refs breaking or being abandoned.

## Facts about the current library (checked 2026-11-20)

- Library root: `~/Library/CloudStorage/Dropbox`. Settings: `~/.config/refs/config.toml`
  (`roots = ["phd", "papers-dump"]`, `dump = "papers-dump"`, `layout = "phd"`).
  Synced records: `Dropbox/phd/tools/refs/{works,reads,journal.jsonl,review.json}`.
  Local index (a rebuildable cache): `~/Library/Application Support/refs/index.db`.
- `~/phd` is a separate local tree (Syncthing, Git repos); `~/phd/dropbox` links to `Dropbox/phd`.
- `Dropbox/phd/research/bets` is a Dropbox **shared folder** (coauthors); `itau` is not.
- Index: 1,570 paths, 1,427 distinct files, 110 sets of identical copies. Most come
  from LaTeX `build/` + `pdfs/` outputs, bCourses exports and coauthors' folders,
  not from refs (the journal has no `copy` actions).
- Readings (kind paper/book/chapter/report) vs other PDFs, by area:

  | Area | Readings | Other |
  |---|---|---|
  | `papers-dump` | 341 | 31 |
  | `phd/coursework` | 246 (6 in bCourses sync folders) | 818 |
  | research / projects / ra | ~99 | ~31 |

- Work records (`works/*.json`) already support one work to many files (`files`:
  list of hashes) and have a `user.tags` field (filled only by the Zotero import).
- `scan.py` already re-finds moved files by inode (+ size + mtime) or by content
  hash, whether or not refs was running. `reading.py` already reads annotations
  per file by author (`marks_by_author`).

## Decisions

1. **One library folder holds every reading.** Flat, canonical file names. Rename
   `papers-dump` to `refs-library`. It is also the quick-add folder.
2. **Courses and projects are collections**, stored in refs' records. A paper can be
   in any number of them. Sub-collections use a slash: `219a/public-economics`.
3. **Shared project folders get copies.** The library always has the paper; a
   shared folder (coauthors) gets a recorded copy. Accept copies there; nowhere else.
4. **Course folders are no longer indexed.** refs watches the library and shared
   collection folders only. Course readings enter the library tagged with the course.
5. **Resilience:** a live `library.bib` export (with `file` and `keywords`) keeps
   every membership in a format other reference managers import. Optional
   `readings.md` per course folder.
6. **Annotations across copies:** flag which copy has whose marks and how recent;
   later, an on-request pull merge into the library copy.
7. **New projects from refs** = create a collection (plus optional folder, shared
   flag, `.bib`). Repo / Overleaf / `local/` scaffolding stays outside refs.

Rejected: symlinks (Dropbox syncs only the link; coauthors in a shared folder get
a link to a file they lack; unsupported officially; refs' scanner does not follow
links; unreliable on iPad), hard links (Dropbox stores them as two files),
"folders are the home, tags on top" (keeps 818 non-readings in the index and
spreads readings across course folders).

## Implementation

### 1. Collections (phase 1)
Feel free to revise implementation details.

- Registry file in the state folder, `collections.json`: per collection `name`
  (slug, e.g. `220a`, `bets`), `kind` (course | project), `term`, `folder`
  (Dropbox-relative, optional), `shared` (bool), `bib_file` (optional), `created`.
- Membership on the work: `user.collections: ["220a", "bets", "219a/public-economics"]`,
  separate from the LLM-derived `tags` (fields, methods) so the two never mix.
  Keep `user.tags` as free personal tags.
- Search: `#220a` and the existing `course:` / `project:` filters resolve against
  collections; a parent matches its sub-collections.
- Web page: collection picker on a paper and on a selection (reuse the
  File to… box and multi-select); collections in the sidebar.
- `refs project new <name> [--course --term T] [--folder P] [--shared] [--bib F]`
  and the same on the page. Optionally creates `phd/research/<name>/readings`.
- Until migration, `contexts.py` keeps deriving course/project from paths; expose
  those as implicit collections so filters keep working.

### 2. `library.bib` export (phase 1)

- Regenerated (debounced, from the watcher) whenever works or collections change.
  Written to the state folder in Dropbox. Each entry: usual fields + `file = {…}`
  (path relative to the .bib, or absolute; pick what JabRef/Zotero import best) +
  `keywords = {220a, bets}`. Reuse `bib.py`.
- Per-collection export already exists (`refs bib project:<slug>`); point it at collections.

### 3. Move-and-edit gap in the scanner (phase 1)

- `scan.py` case B1 requires inode + size + mtime to match a vanished path. A file
  moved **and** annotated between scans fails B1 and B2 and becomes a new paper.
- Fix: when a new path's inode matches a vanished path but size/mtime differ, treat
  it as a move plus a modification: hash it, `_ensure_file(..., supersedes=old_hash)`,
  log both. Add a test in `tests/test_scan.py`.

### 4. Copies in shared folders + annotation flags (phase 2)

- Adding a paper to a `shared` collection copies its library file into the
  collection's folder (reuse `organize.copy`, journaled). A coauthor's new PDF
  in a shared folder is offered for taking into the library (copy) with the
  collection pre-filled. Removing the membership asks before trashing the shared
  copy; never automatic.
- A copy is "recorded" when one location is in the library and another in the
  shared folder of a collection the work belongs to. Derive this; no new state.
  Recorded copies are left out of the Copies review. `refs path`, agents and
  opening a paper always use the library copy.
- Per path, store marks by author and the latest annotation date (annotation
  `modDate`, falling back to file mtime). Show per copy on the paper
  ("library copy: your marks on 14 pages, last 12 Nov · bets copy: 6 pages by
  a coauthor, last 18 Nov ⚑"). Flag when the other copy has newer marks or marks
  the library copy lacks. Read status counts your marks on any copy.

### 5. Narrower watching, tagging on add, course inbox (phase 2)

- Roots become: the library + folders of shared collections. Inboxes (Downloads,
  course folders, bCourses sync) are listed, not indexed.
- Add bar, bookmarklet and Downloads view take collections: `a` then `#220a`;
  the current term's courses are suggested first.
- Course inbox view, modelled on `downloads.py`: paper-like PDFs in course folders
  and `canvas-sync` that the library lacks, with the course pre-filled. Take =
  move, except **copy** from managed folders (`canvas-sync`, `no_rename`), which
  would re-download a moved file.

### 6. Migration of existing readings (phase 3)

- `refs migrate` builds a plan: every reading outside the library -> library path
  (canonical name) + collections from its current context; topic folders
  (`readings/03-demand`, `9 & 10 - Application to Public Economics`) become
  sub-collections. Course material is left where it is and dropped from the index.
- Reviewed on the page before anything moves. Move by default; copy from shared
  folders (~98 in `bets/readings/lit`, so coauthors see no change) and managed
  folders. Every action journaled; undo available. Read status, notes and
  citekeys follow the work.
- Then: switch `roots`, rename `papers-dump` -> `refs-library` (update `dump`, config,
  and anything pointing at the old path), drop `phd/referee` from the index.
- Optional `readings.md` written into each course folder: the course's readings
  by topic (names to look up in the library).

### 7. Annotation pull merge (phase 4)

- "Bring annotations from the <collection> copy": match annotations by `/NM` id,
  else page + type + rect; add those the library copy lacks (keep author,
  content, colour) with PyMuPDF; write only into the library copy, after saving
  the previous version in refs' folder. Refuse if the file changed in the last
  few minutes (Dropbox conflicted copies).
- Limits: deletions do not propagate (start saving a per-scan annotation snapshot
  to allow a three-way merge later); redrawn appearance may differ slightly;
  GoodNotes/Notability annotations are not in the PDF. Pushing into the shared
  copy is a separate, explicit action.

## Phases

1. Collections, `refs project new`, `library.bib`, move-and-edit scanner fix. No files move.
2. Shared-folder copies + annotation flags; narrower roots; collections on add; course inbox.
3. Migration with review and undo; rename `papers-dump` to `refs-library`.
4. Annotation pull merge.

## Phase 1 as built (2026-11-20)

- `collection.py`: registry `<state>/collections.json` keyed by name (kind, term,
  folder, shared, bib_file, created); `user.collections` on works; folder
  contexts give implicit collections (`collection.implicit`). Topic folders are
  *not* implicit sub-collections yet: their names need curating (phase 3).
- Search: `#name` / `collection:` (several `#` must all hold), `course:` and
  `project:` match a collection or the old folder slug. `files.collections`
  is an added index column.
- CLI: `refs collection [list|add|remove|rename|new|delete]`,
  `refs project new`, `refs bib --library`, `refs bib '#x' --append` (the
  collection's .bib). Collection .bib files also feed `bibkeys`.
- Page: collections in the sidebar (projects, then courses by term; each with its own `+ new`),
  chips on a paper, a picker (`#`) for a paper or a selection, "take out of",
  delete an empty collection.
- `librarybib.py`: `<state>/library.bib`, JabRef `file` format, relative path,
  `keywords` = all collections. Written by the watching process (debounced
  poke + a one-minute fingerprint check).
- Scanner rule B4: same inode, new content = moved + modified.
- Decided: the `bets` shared folder for phase 2 is `phd/research/bets/readings/lit`
  (watched and copied into), not the whole project folder. The `roots` switch
  happens at the end of phase 3's migration, not in phase 2.

## Phases 2–4 as built (2026-11-20, one branch with the PR #5 fix)

- Phase 2: `cfg.watched` = roots + shared collections' folders; `inboxes`
  setting (skipped by the scan, listed by `inbox.py`); `shared.py` (copy on
  add, offer to trash on remove, recorded copies out of the Copies review);
  `library.py` (place under the canonical name, journaled); marks per copy
  (`marks_by`, `marks_latest` columns; ⚑ flags); collections on add (CLI
  `--to`, add bar `#name` + Tab, bookmarklet "into" field remembered,
  Downloads field, drop onto a shown collection). Collections list like
  reading lists again (parts carry `order` and `label`).
- Phase 3: `migrate.py` + `refs migrate` + the migration view. Best file per
  paper = most of your marks, then newest. Copy from shared and managed
  folders, move otherwise. Records in `<state>/migrations/` for undo (undo
  before finishing: after the rename the recorded paths are gone). `finish`
  renames the folder and saves dump/roots/inboxes through settings.py.
- Phase 4: `annotations.pull` + `refs marks --pull` + the page button.
  Pushing into the shared copy is not built (the plan's "separate, explicit
  action"); nor the per-scan snapshot for a three-way merge.

## Defaults (confirm or change)

| Decision | Default |
|---|---|
| Library folder name | `refs-library` (decided) |
| Past terms | migrate their readings too, after review |
| Topic folders | sub-collections (`219a/public-economics`) |
| `readings.md` in course folders | on |
| Referee manuscripts | out of the index |
| Finder tags mirroring collections | not planned; test Dropbox sync of Finder tags first |
