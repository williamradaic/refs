# Developing refs

## Setup

```sh
git clone https://github.com/williamradaic/refs && cd refs
uv sync                                  # .venv with the dev tools
uv run pytest                            # the tests (no network, no real library)
uv tool install --editable . --force     # the `refs` command, running this checkout
```

The `refs` command runs from its own environment. Code changes reach it
through the editable install (`refs service restart` to load them into the
background service); a new dependency in `pyproject.toml` needs the
`uv tool install` line again first.

The agent skill lives in `src/refs/skill/SKILL.md` (shipped with the package;
`extras/agent-skill/refs` links to it). For a skill that follows your edits,
link it instead of copying it, which `refs skill install` then leaves alone:

```sh
ln -s "$PWD/src/refs/skill" ~/.claude/skills/refs
ln -s "$PWD/src/refs/skill" ~/.codex/skills/refs
```

## Trying changes without touching your library

`.claude/launch.json` defines four local servers:

| Entry | Port | What it serves |
|---|---|---|
| `refs-dev` | 8378 | your real library and settings, without the folder watcher |
| `refs-sandbox` | 8379 | a fake, set-up library in `local/sandbox/` (its own settings and index), no watcher |
| `refs-first-run` | 8380 | an unconfigured library in `local/first-run/`, with the watcher: the first-run screens |
| `refs-demo` | 8381 | a demo library in `local/demo/` (about 20 well-known economics papers, a few notes, an Obsidian vault): the README screenshots |

The sandboxes are made by hand (`local/` is ignored by Git): a `Dropbox/`
folder with a few PDFs, and for `refs-sandbox` a `config.toml`. The servers
use `REFS_CONFIG`, `REFS_DROPBOX_ROOT` and `REFS_INDEX_DB`, which the tests
use too; the API key still comes from the Keychain, so avoid reading PDFs
there unless you mean to pay for it.

The README screenshots in `docs/screenshots/` are 1440×860 captures at 2× of
`refs-demo`, taken with headless Chrome (deep links such as `#c=bets&w=<id>`
select the view). Never capture your real library for them.

## Map of the code

| Module | Job |
|---|---|
| `config.py` | settings: generic defaults, `~/.config/refs/config.toml`, environment |
| `settings.py` | the settings page's back end: checks a change, writes it with tomlkit (comments kept) |
| `onboarding.py` | first run: library candidates, the folder survey, private-folder and term-rule suggestions |
| `contexts.py` | folders → course or project (`Layout`: own rules, a preset, a generic rule per watched folder) |
| `collection.py` | collections (courses, projects): the registry, memberships on works, the collections folders stand for |
| `librarybib.py` | `library.bib`, every work with its PDF and collections, rewritten by the watcher's process |
| `library.py` | the library folder: which copy is the library's, placing a PDF there under its canonical name (journaled) |
| `shared.py` | collections shared with coauthors: copies in their folders, recorded copies, `collect` (memberships + copies + index) |
| `inbox.py` | papers in inbox folders (listed, not indexed) and shared folders that the library lacks; taking them in |
| `annotations.py` | each copy's marks by author and date, flags, and the pull merge into the library copy |
| `keep.py` | which copy of a paper to keep (marks, version, place, name) and doing it: the Copies review |
| `storage.py`, `scan.py` | walk the watched folders, hash, reconcile with the index |
| `index.py`, `embed.py` | the SQLite index (a rebuildable cache) and semantic search |
| `extract.py`, `llm.py`, `resolve.py`, `pipeline.py` | reading a PDF: text and identifiers, Claude Haiku's transcription, registry lookup, status |
| `works.py`, `records.py` | works (one per paper) and read records, the JSON files kept in the library |
| `ingest.py`, `watch.py` | read new PDFs; follow the folders (FSEvents) |
| `budget.py` | the reading ledger, the monthly limit, approval of large batches |
| `reading.py`, `notes.py` | read status from your markup; Obsidian notes |
| `bib.py`, `bibkeys.py` | BibTeX; keys learned from project `.bib` files |
| `rename.py`, `organize.py` | tidy names, file to / copy to (one or many, new folders), copies (journaled, undoable) |
| `downloads.py` | PDFs in ~/Downloads: which look like papers (read on this Mac, free), taking them into the library, trashing copies refs has |
| `capture.py` | quick add: reading links and identifiers, finding a PDF, the bookmarklet's key |
| `ask.py` | Claude Haiku ranks a shortlist when local search is unsure |
| `doctor.py`, `agents.py`, `service.py`, `keys.py` | `refs doctor` and `uninstall`, the agent skill, the login service, the Keychain |
| `web/app.py`, `web/static/` | the local server and JSON API; the page (plain HTML, CSS and JavaScript, no build step); `bookmarklet.html` builds the bookmarklet, `capture.html` is the window it opens |
| `cli.py` | the `refs` command |

## How it identifies files

A file is its content (SHA-256 hash), not its path. One scan classifies every PDF:

| Situation | How it is recognised | Reported as |
|---|---|---|
| Same path, same size and time | no need to re-read | unchanged |
| New path, inode of a vanished path | the disk's own file ID survived | moved |
| New path, known content, old path vanished | hash | moved |
| New path, known content, old path still there | hash | copy |
| Same path, new content | hash; new version points to the old | modified |
| New path, inode of a vanished path, new content | moved, then edited between scans; new version points to the old | moved + modified |

## How metadata is produced

The rule: the model never authors a bibliographic fact.

1. `extract.py` reads the first three pages and any identifier printed on the
   first two (DOI, arXiv, NBER, SSRN, JSTOR).
2. `llm.py` has Claude Haiku transcribe kind, title, authors, year and venue as
   printed, and write a description and tags. It never sees the file name.
3. `resolve.py` looks the identifier up, or searches Crossref and OpenAlex for
   the transcribed title. A candidate is accepted only if the title is at least
   90% similar and at least half the printed authors appear; the right version
   is then chosen by year (published article vs working paper vs reprint).
4. `pipeline.py` assigns a status (README, "The record status") and saves one
   record per file hash.

Known gap: many pre-2000 articles (old *AER* issues on JSTOR, for instance)
have no usable registry record and end as extracted or needs-review.

## Where things live

| What | Where |
|---|---|
| Settings | `~/.config/refs/config.toml` (`.bak` = the version before the last save) |
| Index (rebuildable cache), search vectors, Haiku answers, reading ledger | `~/Library/Application Support/refs/index.db` |
| Search model (about 70 MB, downloaded once), registry cache | `~/Library/Caches/refs/` |
| Works, read records, rename journal, `review.json`, `collections.json`, `library.bib`, annotation backups (synced) | `<library>/<state_dir>/`, `.refs/` by default |
| Reading notes | `<vault>/<obsidian_folder>/<citekey>.md` |
| API key | macOS Keychain, item `refs` |
| Login service, log | `~/Library/LaunchAgents/com.williamradaic.refs.plist`, `~/Library/Logs/refs.log` |

The JSON files in the library carry a data version (`"schema"`,
`records.SCHEMA`). Raise it when their shape changes in a way an older refs
would get wrong, and teach `records.read_json` to upgrade older files. refs
never overwrites a file from a newer version (`records.NewerData`), so an old
copy on a second Mac cannot undo a newer one's data. The index has its own
`SCHEMA_VERSION` and is simply rebuilt when it changes.

## Collections

A work's collections are two lists, merged wherever they are shown:

- **explicit**, `user.collections` in the work file: what you put it in. Kept
  apart from `tags` (the model's fields and topics) and `user.tags` (free tags,
  from Zotero).
- **from folders**: each PDF's context (`contexts.py`) stands for a collection
  of the same name, `collection.implicit()` (`phd/coursework/2026-fall/220a` →
  `220a`, a course). These keep courses and projects working while readings
  still live in course folders; they cannot be removed except by moving the file.

`index.refresh_derived` writes the merged list, per work, into
`files.collections` (an added column, so no rebuild), and the `#name` /
`collection:` / `course:` / `project:` filters read it; a parent matches its
parts. A page or CLI change updates the column at once
(`index.update_collections`), without a scan. `collections.json` (the
registry) holds only details: kind, term, folder, shared, `bib_file`. A name
can have members without an entry.

`library.bib` runs in the watching process only (one per Mac): the watcher
pokes it after every pass (the first one, at start, fills the index), the page after any edit, and it
compares a fingerprint of the work files once a minute for changes from the
command line or another Mac. The `file` field is JabRef's `:path:PDF` with a
path relative to the .bib; the PDF is the copy in the quick-add (library)
folder when there is one.

### Watched folders, inboxes, the library folder

`cfg.watched` is what is indexed: `roots` plus the folders of shared
collections (read from `collections.json` when the settings load; the server
reloads them when the registry changes them). `inboxes` are skipped by the
walk and listed by `inbox.py` instead, judged like Downloads (hash + verdict,
remembered while unchanged); a file read before keeps its work through the
`files` row, which outlives its paths. The library folder is `dump`;
`index.search` puts its copy first in every result's locations.

Per file the index also keeps `marks_by` (everyone's marks by author) and
`marks_latest`; files counted before those columns existed are counted again
once. PyMuPDF marks hold only a weak link to their page: keep the page
objects alive while working with marks (`annotations._marks`).

## Calibration

Several numbers were set on the author's library and may want revisiting on
others: the similarity cut-offs for semantic search and for asking
(`index.py`, `ask.py`, with the small model), the read-status thresholds
(`reading.py`), the private-folder name patterns (`onboarding.py`), and the
field vocabulary given to the model (`llm.py`, economics).
`scripts/measure_identifiers.py` reports how many papers carry an identifier a
registry can resolve (about a third, on the first run).

## The bookmarklet

Publishers turn refs' own requests away (OUP answers 403) or answer with a
paywall page, while the browser is signed in and on the library's network. So
the bookmarklet does the reading in the page: the `citation_*` meta tags, and
`fetch` of `citation_pdf_url` with your cookies (same site only; a PDF that
redirects to another site, as OUP's does, is refused by the browser). It
opens `/capture` and hands the result over with `postMessage` once that window
says it is ready. `/api/capture` starts `refs add` at once only with the key
built into the bookmarklet (`capture.token`, one per Mac, next to the index):
any website can open the window, none can read your bookmarks. Without the key,
or without a message (a site whose Cross-Origin-Opener-Policy cuts the link),
the window falls back to adding the address after Enter. With a library proxy
set, a paywall answer sends the page's tab through it.

The built-in browser pane of the Claude app cannot open popups, so it only
exercises that fallback; the handover itself is tested by calling the capture
page's `fromBookmarklet` with a message (tests/test_capture.py covers the API).
