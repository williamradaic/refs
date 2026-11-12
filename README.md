# refs

A folder-aware reference manager that sits on top of Dropbox. PDFs stay where
they are, in course and research folders; `refs` indexes them, recognises which
course or project each one belongs to, and follows renames, moves and copies.

Status: **Phase 1 of 4 done** (read-only engine and command line). Phase 2 has
started: `extract.py` reads identifiers off a PDF's opening pages and
`resolve.py` looks them up in Crossref and arXiv; neither is wired into `refs`
yet. The full plan is in `local/admin/plan.md`.

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
```

Filters for `find`: `course:` / `project:` (folder slug), `term:`, `area:`,
`in:` (context contains), `topic:`, `section:`, `kind:`, `year:`, `author:`.
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

## Where things live

| What | Where |
|---|---|
| Code | `~/phd/tools/refs` (this repo) |
| Index (rebuildable cache) | `~/Library/Application Support/refs/index.db` |
| Library records (from Phase 2) | `Dropbox/phd/tools/refs/`, reachable here as `dropbox/` |
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
