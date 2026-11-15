# refs

A folder-aware reference manager that sits on top of Dropbox. PDFs stay where
they are, in course and research folders; `refs` indexes them, recognises which
course or project each one belongs to, and follows renames, moves and copies.

Status: **Phases 1–4 built**: engine, metadata pipeline, works, quick add and
bookmarklet, live folder watching, launch at login, the web UI with a Review
queue (records, renames, copies), "file to…" and "copy to…", BibTeX with `--append`, keys
learned from project `.bib` files, and an agent skill. Dropped: first-page
thumbnails and a Raycast "add the front tab" command (the bookmarklet covers
it). What remains is the plan's "Later" list, in `local/admin/plan.md`.

## Use

```sh
refs scan                      # bring the index in line with Dropbox
refs status                    # what the library holds
refs find blp                  # search titles, authors, initials, file names, notes
refs find course:220a kind:paper topic:demand
refs find IO paper on ice cream   # a description works too (matched by meaning)
refs ask demand for frozen desserts   # let Haiku pick from ~40 candidates (~$0.0005)
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
refs add w32515 --queue        # ... and put it on the reading queue
refs queue                     # the reading queue, oldest first
refs queue blp                 # queue the best match (--remove takes it off)
refs wanted                    # works saved without a PDF yet
refs bib blp                   # BibTeX for the best match (or `refs bib project:bets`)
refs bib blp --append overleaf/references.bib   # add it unless the file already has it
refs rename in:dump            # propose canonical names; add --apply to rename
refs rename --undo 3           # reverse the last 3 renames

refs serve --open              # the web interface, at http://127.0.0.1:8377 (also watches Dropbox)
refs watch                     # watch Dropbox without the web interface; --once for one pass
refs zotero-import --dry-run   # what importing ~/Zotero would do; drop --dry-run to do it
```

The web interface is keyboard-driven (`j`/`k` to move, `/` to search, `?` for
all keys) and lives in `src/refs/web/`: `app.py` is the local server and JSON
API, `static/` holds the page (plain HTML, CSS and JavaScript, no build step).

Review, in the sidebar:

| View | What you do there |
|---|---|
| needs review | correct a record, fetch it by DOI, or accept it |
| renames | every file not named `surname-year-short-title.pdf`, grouped by folder; tick files (or a folder) and rename them; undo is offered right after. Protected folders (canvas-sync, overleaf, Git repositories) never appear |
| copies | the same PDF in several places: move a copy to the macOS Trash (never the last one), or "keep all" to say the copies are on purpose (remembered in `review.json`) |

**Notes** (`n`, or click the box in the right panel): your reading notes on a
paper. Each note is a Markdown file in the Obsidian vault,
`~/wr-vault/phd/refs/<citekey>.md`, so it is in Obsidian (and on the phone)
too, and `[[citekey]]` links to it from any other note. refs writes the
properties (title, authors, year, doi, citekey, an author-year alias) when it
creates the file and never changes them afterwards; the panel edits only the
text below. It saves a moment after you stop typing, on `⌘S`, and when you
leave the box (`esc`). If the file changed in Obsidian since the panel loaded
it, the save is refused rather than overwriting; the panel says so and offers
Obsidian's version. Keep the file name: refs finds the note by citekey, so a
renamed note is a new, empty one to refs. Notes are searched like everything
else (by word and by meaning); an edit made in Obsidian is picked up a few
seconds later while refs runs. Settings: `obsidian_vault` and
`obsidian_folder` in `config.toml`.

**Queue** (`q`, or the button under BibTeX): papers you mean to read next.
Queued papers carry an accent-coloured ▸ in the list's first column (it
takes the place of ◐ or ●), and
"queue" in the sidebar lists them in the order you queued them. In the add
bar, shift+Enter adds a paper and queues it. A paper leaves the queue when it
is read: when you mark it (`r`), or when your markup shows you read it. The
queue time is stored in the work file (`user.queued`), so it syncs.

**Read status** is unread, reading (◐ in the first column) or read (●). You set it with `r`; otherwise refs works it out from your own
markup in the PDF (`src/refs/reading.py`):

| Your highlights, underlines, notes or ink | Status |
|---|---|
| on at least 30% of the pages, and on at least 5 pages (half the pages of a shorter PDF) | read |
| on 2 or more pages | reading |
| on 1 page, or none | no change |
| any, in a PDF of more than 150 pages (a book) | reading at most |

Pages with marks count, not marks: you may highlight one paragraph in ten
pieces. Only marks whose author is you count, so an instructor's highlights on
a course reading do not; the names are `annotation_authors` in `config.toml`
(Preview writes your macOS name, the iPad apps the device name; the default
is your macOS name only). What you set
always wins, and detection only ever raises a status. The detail panel says
where a status came from ("from your markup on 39/57 pages"). Each PDF is read
for marks once per content (about 4 ms); annotating it changes the content, so
the next scan counts it again. The detected status is stored in the work file
(`read_auto`). The sidebar lists unread, reading and read papers. Filters:
`read:read`, `read:reading`, `read:unread`, `queued:yes`.

**File to…** (`F`, or the link under each file) moves a PDF into another folder,
typically out of `papers-dump` into a course or project. It is the only way refs
moves a file; the move is journaled, so `refs rename --undo 1` reverses it.

**Copy to…** (`C`, or the link next to it) does the same but leaves the original
where it is, for a PDF that has to sit in a shared project folder. The pair is
marked as kept on purpose, so it stays out of Review > copies. Undo sends the copy
to the Trash, and only while the original still exists.

**Bookmarklet**: open `http://refs.localhost:8377/bookmarklet` and drag
"+ refs" to the bookmarks bar. On a paper's page it opens refs with the link
filled in; Enter adds it.

**Project keys.** refs reads your project `.bib` files (read only) and
remembers the key each one uses for a library paper, so `refs find o2006optimal`
finds O'Donoghue and Rabin (2006) even though refs calls it
`odonoghue2006optimal`. The detail panel lists these keys with their file, and
`refs status` lists the files read. Which files: `bib_files` in `config.toml`
(none by default; list your own projects' files, not course ones, whose keys
are someone else's). Matching is by DOI, or by
exactly the same title; keys are recomputed on every scan.

**`--append`** never adds a paper the `.bib` already holds under any key
(matched by key, DOI or title); it lists those with the key the file uses. For
a `.bib` inside `overleaf/`, pull first and commit and push afterwards.

**Search** combines two rankings (`index.search`, `embed.py`, `ask.py`):

| | What it does | Cost |
|---|---|---|
| words | every word must appear (as a prefix) in the title, authors, initials, citekeys, file names, tags, summary or your note; `IO`, `PF`, `RDD`, … also match the spelled-out field | free, instant |
| meaning | for queries of two or more words: a small model on this Mac (`BAAI/bge-small-en-v1.5` through fastembed) compares the query with each paper's title, tags, summary and note, so "frozen desserts" finds an ice-cream paper. Names and identifiers alone ("chetty 2009", "blp") skip this step | free, ~10 ms |
| Haiku | ranks a shortlist of ~40 local candidates and says why each fits | ~$0.0005 a question |

The two free rankings are merged (reciprocal rank fusion), so exact matches
stay on top. Haiku is asked only when a query reads like a description **and**
the local result looks unsure (no paper contains every word, and the best
match by meaning is weak or no clear winner stands out): in the web page when
you press Enter (typing stays local), and in `refs find` at the terminal. A
query starting with `?`, or `refs ask`, always asks; `--local` and `--json`
never ask on their own. Haiku sees titles, authors, tags and summaries of the
candidates, never your notes or anything from `no_llm` folders. Answers are
cached by query, and the month's spending is capped (`ask_monthly_cap`,
default $1; then search stays local). Settings in `config.toml`: `ask =
"never"` turns automatic asking off; `embedding_model` changes the model
(`"BAAI/bge-base-en-v1.5"` is larger and somewhat better), and the next scan
re-embeds everything (about 30 seconds). The similarity cut-offs in
`index.py` and `ask.py` were set on this library with the small model.

Filters for `find`: `course:` / `project:` (folder slug), `term:`, `area:`,
`in:` (context contains), `topic:`, `section:`, `kind:`, `year:`, `author:`,
`status:` (any status in the table below, or unprocessed), `tag:`,
`read:` (read, reading, unread), `queued:` (yes, no).
Add `--json` to `find`, `status`, `contexts`, `dupes` or `scan` for machine-readable
output; this is how coding agents query the library.

## Following Dropbox

While refs runs (`refs serve`, so also the login service) it watches the
scanned folders (`src/refs/watch.py`). A few seconds after changes settle it
scans, then reads any PDF it has never read (`src/refs/ingest.py`, the same
steps as `refs add`):

| Where the new PDF is | What happens |
|---|---|
| `papers-dump` (saved from a browser, Finder, Dropbox on the phone) | read, filed under its work, renamed if verified or confirmed; a notification says what arrived |
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

| Status | In the list | Meaning |
|---|---|---|
| verified | ■ | a registry record agrees with what is printed |
| extracted | ◧ | no registry record; metadata is the model's transcription |
| needs-review | ! | something does not add up |
| classified | · | not a library item (slides, problem set, ...) |
| confidential | · | in a no-LLM folder (`phd/referee`); printed identifiers only |
| confirmed | ▣ | you accepted or corrected the record in the web page; renamed like verified |
| imported | ◆ | came from Zotero and has not been read by the pipeline |
| no-file | □ | a work saved without a PDF (`refs wanted`); the PDF attaches when it arrives |

Status marks are squares; the circles in the column before them are your
reading (◐ started, ● read).

Known gap: many pre-2000 articles (old *AER* issues on JSTOR, for instance) have
no usable registry record and end as extracted or needs-review.

## Where things live

| What | Where |
|---|---|
| Code | `~/phd/tools/refs` (this repo) |
| Index (rebuildable cache), incl. search vectors and Haiku answers with their cost | `~/Library/Application Support/refs/index.db` |
| Embedding model (64 MB, downloaded once) | `~/Library/Caches/refs/fastembed/` |
| Works, one per paper (citekey, your read status, queue and tags) | `Dropbox/phd/tools/refs/works/` |
| Reading notes, one per paper | `~/wr-vault/phd/refs/<citekey>.md` (Obsidian Sync) |
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
| Settings | `~/.config/refs/config.toml` (contact email, extra exclusions, `auto_process`) | by hand |
| API key | macOS Keychain, item `refs` | `refs setup-key` |
| Index (rebuildable) | `~/Library/Application Support/refs/` | `refs scan` |
| Registry cache, embedding model | `~/Library/Caches/refs/` | lookups, the first scan |
| Library data (synced) | `Dropbox/phd/tools/refs/`: `reads/`, `works/`, `journal.jsonl`, `review.json` | the pipeline |
| Reading notes | `~/wr-vault/phd/refs/` | the notes box in the web page |
| Agent skill | `~/.claude/skills/refs` and `~/.codex/skills/refs`, links to `extras/agent-skill/refs` | by hand (see New Mac) |
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
ln -s ~/phd/tools/refs/extras/agent-skill/refs ~/.claude/skills/refs   # agent skill
ln -s ~/phd/tools/refs/extras/agent-skill/refs ~/.codex/skills/refs
```

Optional: `contact_email = "..."` in `~/.config/refs/config.toml`; import
`extras/raycast-quicklinks.json` in Raycast.

### Remove it

```sh
refs service uninstall
uv tool uninstall refs
rm -rf ~/Library/Application\ Support/refs ~/Library/Caches/refs ~/Library/Logs/refs.log ~/.config/refs
rm ~/.claude/skills/refs ~/.codex/skills/refs
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

The agent skill (`extras/agent-skill/refs/SKILL.md`) tells Claude Code and
Codex how to use `refs find --json`, `refs bib` and `refs add`, and what to
leave to you. Edit it there; the links pick up changes.

`scripts/measure_identifiers.py` reports how many papers carry an identifier a
registry can resolve (about a third, as of the first run).
