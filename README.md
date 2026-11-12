# refs

A folder-aware reference manager that sits on top of Dropbox. PDFs stay where
they are, in course and research folders; `refs` indexes them, recognises which
course or project each one belongs to, and follows renames, moves and copies.

Status: **Phases 1 and 2 of 4 built** (engine, metadata pipeline, works, quick
add, rename with undo, BibTeX). Next: the web UI and live folder watching. The
full plan is in `local/admin/plan.md`.

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
```

Filters for `find`: `course:` / `project:` (folder slug), `term:`, `area:`,
`in:` (context contains), `topic:`, `section:`, `kind:`, `year:`, `author:`,
`status:` (verified, extracted, needs-review, classified, unprocessed), `tag:`.
Add `--json` to `find`, `status`, `contexts`, `dupes` or `scan` for machine-readable
output; this is how coding agents query the library.

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

Defaults (`src/refs/config.py`): scans `Dropbox/phd` and `Dropbox/papers-dump`;
never enters `phd/admission` or `phd/move-in` (personal paperwork), figure
folders, `_versions`, or its own state folder. Online-only Dropbox files are
listed but not read, because reading them would force a download.

## Develop

```sh
uv sync            # create .venv with the dev tools
uv run pytest      # run the tests
uv tool install --editable .   # (re)install the `refs` command
```

`scripts/measure_identifiers.py` reports how many papers carry an identifier a
registry can resolve (about a third, as of the first run).
