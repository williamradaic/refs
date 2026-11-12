# refs

A folder-aware reference manager that sits on top of Dropbox. PDFs stay where
they are, in course and research folders; `refs` indexes them, recognises which
course or project each one belongs to, and follows renames, moves and copies.

Status: **Phases 1–3 of 4 built**: engine, metadata pipeline, works, quick
add, rename with undo, BibTeX, the web UI, live folder watching and launch at
login. Still open from Phase 3: the bookmarklet and first-page thumbnails.
Next: Phase 4 (filing out of the dump, rename approval in the UI, project
`.bib` files). The full plan is in `local/admin/plan.md`.

## Use

```sh
refs scan                      # bring the index in line with Dropbox
refs status                    # what the library holds
refs find blp                  # search titles, authors, initials, file names
refs find course:220a kind:paper topic:demand
refs contexts                  # courses and projects recognised from folders
refs dupes                     # files stored in more than one place
refs open blp                  # open the best match
refs path blp                  # print its full path

refs setup-key                 # store the Anthropic API key in the macOS Keychain
refs process course:220a       # read + look up the selected files (uses the API)
refs process --all             # everything not yet processed
refs process in:dump --relookup   # redo only the free registry step
refs eval course:220a          # compare results with convention-style file names

refs add w32515                # quick add: NBER number, arXiv id, DOI, URL or a PDF file
refs wanted                    # works saved without a PDF yet
refs bib blp                   # BibTeX for the best match (or `refs bib project:bets`)
refs rename in:dump            # propose canonical names; add --apply to rename
refs rename --undo 3           # reverse the last 3 renames

refs serve --open              # the web interface, at http://127.0.0.1:8377 (also watches Dropbox)
refs watch                     # watch Dropbox without the web interface; --once for one pass
refs zotero-import --dry-run   # what importing ~/Zotero would do; drop --dry-run to do it
```

The web interface is keyboard-driven (`j`/`k` to move, `/` to search, `?` for
all keys) and lives in `src/refs/web/`: `app.py` is the local server and JSON
API, `static/` holds the page (plain HTML, CSS and JavaScript, no build step).

Filters for `find`: `course:` / `project:` (folder slug), `term:`, `area:`,
`in:` (context contains), `topic:`, `section:`, `kind:`, `year:`, `author:`,
`status:` (verified, extracted, needs-review, classified, unprocessed), `tag:`.
Add `--json` to `find`, `status`, `contexts`, `dupes` or `scan` for machine-readable
output; this is how coding agents query the library.

## Following Dropbox

While refs runs (`refs serve`, so also the login service) it watches the
scanned folders (`src/refs/watch.py`). A few seconds after changes settle it
scans, then reads any PDF it has never read (`src/refs/ingest.py`, the same
steps as `refs add`):

| Where the new PDF is | What happens |
|---|---|
| `papers-dump` (saved from a browser, Finder, Dropbox on the phone) | read, filed under its work, renamed if verified; a notification says what arrived |
| anywhere else under the roots | read and filed; never renamed |
| moved, renamed, copied or annotated | recognised from the scan; nothing is read again |
| still downloading (no `%%EOF` yet) | left until the download finishes |

At startup it does one pass, which catches whatever changed while it was off.
A file whose reading failed is not retried automatically (`refs process` does
that). `auto_process` in `~/.config/refs/config.toml` sets what is read:
`"all"` (default, about $0.001 a file), `"dump"`, or `"none"`. Online-only
Dropbox files are not read until they are downloaded. Only one refs process
watches at a time, and an ingest from `refs add` and one from the watcher
never run together, so no file is paid for twice. The web page reloads its
list by itself when something changed.

## Launching

- At login: `refs service install` keeps refs running in the background
  (`status`, `restart` after code changes, `uninstall` to remove).
- Address: `http://refs.localhost:8377` (any `.localhost` name points at this Mac).
  `/?q=blp` opens with a search; `/?add=<link>` opens the add bar filled in.
- Raycast: run **Import Quicklinks** and choose `extras/raycast-quicklinks.json`
  for three quicklinks: `refs`, `refs search`, `refs add`.
- As an app: in Chrome, ⋮ → Cast, Save and Share → Install page as app.

## How it identifies files

A file is its content (SHA-256 hash), not its path. One scan classifies every PDF:

| Situation | How it is recognised | Reported as |
|---|---|---|
| Same path, same size and time | no need to re-read | unchanged |
| New path, inode of a vanished path | the disk's own file ID survived | moved |
| New path, known content, old path vanished | hash | moved |
| New path, known content, old path still there | hash | copy |
| Same path, new content | hash; new version points to the old | modified |

Contexts come from folders (`src/refs/contexts.py`):
`phd/coursework/2026-fall/220a/readings/03-demand-estimation-ii/` is course
`220a`, term `2026-fall`, topic 3 "Demand estimation II".

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
4. `pipeline.py` assigns a status and saves one record per file hash:

| Status | Meaning |
|---|---|
| verified | a registry record agrees with what is printed |
| extracted | no registry record; metadata is the model's transcription |
| needs-review | something does not add up |
| classified | not a library item (slides, problem set, ...) |
| confidential | in a no-LLM folder (`phd/referee`); printed identifiers only |

Known gap: many pre-2000 articles (old *AER* issues on JSTOR, for instance) have
no usable registry record and end as extracted or needs-review.

## Where things live

| What | Where |
|---|---|
| Code | `~/phd/tools/refs` (this repo) |
| Index (rebuildable cache) | `~/Library/Application Support/refs/index.db` |
| Works, one per paper (citekey, your read status and tags) | `Dropbox/phd/tools/refs/works/` |
| Rename journal | `Dropbox/phd/tools/refs/journal.jsonl` |
| Read records, one per file | `Dropbox/phd/tools/refs/reads/`, reachable here as `dropbox/reads/` |
| API key | macOS Keychain, item `refs` |
| Registry response cache | `~/Library/Caches/refs/http/` |
| Settings override (optional) | `~/.config/refs/config.toml` |

Defaults (`src/refs/config.py`): scans `Dropbox/phd` and `Dropbox/papers-dump`
(not `~/phd`, so `~/phd/admin` is never seen); never enters `phd/admission` or
`phd/move-in` (personal paperwork), any folder named `admin` at any depth,
figure folders, `_versions`, or its own state folder. Online-only Dropbox files
are listed but not read, because reading them would force a download.

To keep more away from refs, list folders or single files under
`exclude_paths` in `~/.config/refs/config.toml`; they are added to the
defaults, never in place of them. Or put paperwork in a folder named `admin`.
An excluded file disappears from the index on the next scan; whatever was
already read from it stays in `dropbox/reads/` until you delete that record.

## What refs puts on this Mac

Everything outside this repository, so it can be tracked and removed.

| What | Where | Created by |
|---|---|---|
| The `refs` command | `~/.local/bin/refs` (a uv tool, editable install of this repo) | `uv tool install --editable .` |
| Login service | `~/Library/LaunchAgents/com.williamradaic.refs.plist` | `refs service install` |
| Service log (incl. what the watcher did) | `~/Library/Logs/refs.log` | the service |
| Settings | `~/.config/refs/config.toml` (contact email for registries) | by hand |
| API key | macOS Keychain, item `refs` | `refs setup-key` |
| Index (rebuildable) | `~/Library/Application Support/refs/` | `refs scan` |
| Registry cache | `~/Library/Caches/refs/` | lookups |
| Library data (synced) | `Dropbox/phd/tools/refs/`: `reads/`, `works/`, `journal.jsonl` | the pipeline |
| Optional | a `refs.test` line in `/etc/hosts`; Raycast quicklinks; a Chrome "installed app" | by hand |

It listens on `127.0.0.1:8377` only. It never modifies Zotero, and it renames
files only inside `papers-dump` or when you pass `--apply`.

### New Mac

```sh
gh repo clone williamradaic/refs ~/phd/tools/refs
cd ~/phd/tools/refs
ln -s ~/Library/CloudStorage/Dropbox/phd/tools/refs dropbox   # the synced library data
uv tool install --editable .
refs setup-key          # paste the Anthropic API key
refs scan               # rebuilds the index; papers already read are not paid for again
refs service install    # run at login
```

Optional: `contact_email = "..."` in `~/.config/refs/config.toml`; import
`extras/raycast-quicklinks.json` in Raycast.

### Remove it

```sh
refs service uninstall
uv tool uninstall refs
rm -rf ~/Library/Application\ Support/refs ~/Library/Caches/refs ~/Library/Logs/refs.log ~/.config/refs
```

Then delete the Keychain item `refs` in Keychain Access. The library data in
Dropbox and your PDFs are left untouched.

## Develop

```sh
uv sync            # create .venv with the dev tools
uv run pytest      # run the tests
uv tool install --editable . --force   # (re)install the `refs` command
```

The `refs` command runs from its own environment. Code changes reach it
through the editable install (`refs service restart` to load them), but a new
dependency in `pyproject.toml` needs the `uv tool install` line again first.

`scripts/measure_identifiers.py` reports how many papers carry an identifier a
registry can resolve (about a third, as of the first run).
