# refs

A reference manager for the PDFs you already keep in folders: Dropbox, iCloud
Drive, or any folder on your Mac. Your files stay where they are. refs indexes
them, works out what each paper is (checked against Crossref and OpenAlex),
recognises which course or project it belongs to from its folders, and follows
your folders as you add, move, rename and annotate files.

- Search by words or by meaning ("IO paper on ice cream"), with a
  keyboard-driven page at `http://refs.localhost:8377`.
- BibTeX for any paper or project, and the citation keys your own `.bib`
  files use.
- Add a paper from a link, DOI, arXiv id or NBER number; a bookmarklet adds the
  page you are on.
- A reading queue, read status detected from your own highlights, and reading
  notes kept in Obsidian.
- Coding agents (Claude Code, Codex) can look papers up with it.

refs runs on macOS. It was built by an economist for economists: the fields it
tags papers with (`llm.py`) are those of economics, though any academic PDFs
work.

## Install

You need [uv](https://docs.astral.sh/uv/) (`brew install uv`) and an
[Anthropic API key](https://console.anthropic.com/settings/keys): refs uses
Claude Haiku to read each new PDF, about $0.001 a paper, billed to your key.
Obsidian is optional.

```sh
uv tool install git+https://github.com/williamradaic/refs
refs service install
```

`refs service install` keeps refs running in the background (and starts it at
login), then opens the page. To update later: `uv tool upgrade refs`, then
`refs service restart`.

## First run

While refs is not set up, the page asks:

1. **Library folder**: where your papers are (Dropbox, iCloud Drive, Google
   Drive, OneDrive, Documents, or a folder you choose).
2. **Folders with papers**: a tree of your folders with PDF counts; tick the
   ones refs should watch. Folders of term folders (`Fall 2026`,
   `2025-spring`) are recognised, and the courses inside them are grouped by
   term.
3. **Quick-add folder**: where added papers are saved (created if new).
4. **Private folders**: paperwork to leave out entirely, and confidential work
   (a manuscript you referee) to index but never send to Claude. refs
   suggests some from their names (`visa`, `referee`, ...).
5. **Notes**: an Obsidian vault for reading notes, or none.
6. **Reading**: your API key, and how much to read: everything now (with the
   cost), only papers you add from now on, or nothing.

`refs init` asks the same questions in the terminal. Everything can be changed
later under **settings**. If something seems off, `refs doctor` checks every
part and says how to fix it.

## Daily use

The page is keyboard-driven: `j`/`k` move, `/` searches, `⏎` opens the PDF,
`a` adds a paper, `,` opens settings, `?` lists every key.

**Adding papers.** Paste a link, DOI, arXiv id or NBER number into the add bar
(`a`), or drop PDFs on the page. A PDF you save into the quick-add folder
yourself (from the browser, Finder, or Dropbox on the phone) is picked up on
its own. refs downloads the PDF when it is openly available; a paywalled paper
is saved without one and attaches itself when you save the PDF into the
quick-add folder later. Shift+Enter in the add bar also puts the paper on your
reading queue.

**From Downloads, in a batch.** After a literature search that left a pile of
PDFs in `~/Downloads`, open **downloads** (sidebar, under Review). It lists the
PDFs saved there, newest first, sorted into *paper*, *maybe* and the rest
(receipts, tickets, statements, behind "show other PDFs"). The sorting reads the
PDFs' first and last pages on your Mac, for free: an identifier, an abstract,
a reference list, the length. Tick the ones you want (or "select all"), then
**take into the quick-add folder**, or **take into folder…** to put them
straight into a course or project folder (a new one if you type a path).
They are moved out of Downloads, or copied if you choose copy, then read like
any new paper. A PDF refs already has is left where it is and reported. The
folder is a setting (Library → Downloads folder); empty turns the view off.

**The bookmarklet** (`/bookmarklet`, drag "+ refs" to the bookmarks bar) adds
the paper whose page you are on, in one click. It reads the page's citation
details and fetches the PDF in your browser, signed in as you, so it works
where refs alone is turned away (AEA and OUP pages, library access); a small
window follows the add and closes itself. On a paywall, with a **library
proxy** set under settings (UC Berkeley:
`https://libproxy.berkeley.edu/login?qurl=`), it reopens the page through the
proxy: sign in if asked and click "+ refs" again. Journals that serve the PDF
from another site (OUP's) need the PDF open: click "+ refs" on the PDF itself.
The bookmarklet carries a key belonging to this Mac's refs; after updating refs
from an older version, drag it to the bookmarks bar again.

**Search** combines two free rankings and, when unsure, asks Claude:

| | What it does | Cost |
|---|---|---|
| words | every word must appear (as a prefix) in the title, authors, initials, citekeys, file names, tags, summary or your note; `IO`, `PF`, `RDD`, … also match the spelled-out field | free |
| meaning | a small model on your Mac compares a description with each paper's title, tags, summary and note, so "frozen desserts" finds an ice-cream paper | free |
| Claude Haiku | ranks a shortlist of about 40 local candidates and says why each fits; only when you press Enter on a description and the local result looks unsure (or the query starts with `?`) | about $0.0005, capped at $1 a month |

Filters: `course:` / `project:` (folder name), `term:`, `area:`, `in:` (part of
a folder path), `topic:`, `kind:`, `year:`, `author:`, `tag:`, `status:`,
`read:` (read, reading, unread), `queued:` (yes, no).

**The record status** in the list says how far to trust a paper's details:

| Mark | Status | Meaning |
|---|---|---|
| ■ | verified | a Crossref or OpenAlex record agrees with what is printed in the PDF |
| ▣ | confirmed | you accepted or corrected the record |
| ◧ | extracted | no registry record; the details are Claude's transcription of the first page (drafts, job market papers) |
| ! | needs review | something does not add up; check it under Review |
| · | classified | not a library item (slides, problem sets, ...) |
| · | confidential | in a private folder; read from printed identifiers only |
| ◆ | imported | came from Zotero and has not been read yet |
| □ | no file | saved without a PDF yet |

**Reading.** Each paper is unread, reading (◐) or read (●). Set it with `r`;
otherwise refs works it out from your own highlights, underlines, notes and
ink: marks on at least 30% of the pages (and at least 5) mean read, marks on 2
or more pages mean reading. Only your marks count, not an instructor's: the
names that are you are set under settings ("find names in my PDFs"). `q` puts
a paper on the reading queue (▸); it leaves the queue once read.

**Notes** (`n`): a Markdown file per paper in your Obsidian vault, named by
citekey, so `[[citekey]]` links to it from any note. The page edits the text;
refs writes the properties once and never overwrites a change made in
Obsidian.

**Review**, in the sidebar: *needs review* (correct a record, fetch it by DOI,
or accept it), *renames* (tidy file names, `surname-year-short-title.pdf`,
only for files you tick; undo is offered), *copies* (the same PDF in several
places: trash a copy or keep them on purpose).

**Filing into courses and projects.** **File to…** (`F`) moves a PDF into
another folder, **Copy to…** (`C`) copies it (the original stays, say in a
shared project folder). For many papers at once, select them first: `x` or
⌘-click adds a row, ⇧-click a range, ⌘A everything listed; the selection
survives searches, so you can gather papers from several. In the folder box,
↑/↓ choose and Tab on a folder, then a name, makes a new folder inside it.
`u` undoes the last filing. Besides *added* (the PDF's own date), the *in refs*
column says when a paper entered refs.

**Citing.** `b` copies the BibTeX of the selected paper; `refs bib` does the
same at the terminal, for one paper or a whole project, and `--append` adds
entries to a `.bib` unless it already has them under any key. refs reads your
project `.bib` files (listed under settings; read only) and learns the keys
they use, so `refs find o2006optimal` finds the paper refs calls
`odonoghue2006optimal`.

**Agents.** `refs skill install` teaches Claude Code and Codex to look papers
up with refs (`refs find --json`, `refs bib`, `refs add`) and what to leave to
you.

**Elsewhere.** `/?q=<search>` opens the page with a search, `/?add=<link>` with
the add bar filled in. In Chrome, ⋮ → Cast, Save and Share → Install page as
app gives refs its own window. Raycast users can import
`extras/raycast-quicklinks.json`.

## Courses and projects

refs reads a PDF's course or project off its folders. By default each folder
directly inside a watched folder is one (`Papers/IO/x.pdf` → `Papers/IO`).
Rules under settings say more, tried in order:

| Setting | What it does |
|---|---|
| `contexts` | your own rules, e.g. `Classes/{term}/{course}`, `Research/{name}` |
| `layout = "phd"` | a ready-made layout: `phd/coursework/<term>/<course>`, `phd/research/<project>`, `phd/referee/<journal>/<manuscript>`, … |
| `material_dirs` | folder names whose PDFs are course material, not papers (a course-site sync folder) |

`{term}` matches `2026-fall`, `Fall 2026` and similar; `{course}` matches
`240a` or `1-Econ 240A` (read as `240a`); `{name}` matches any folder; `{area}`
matches any folder and names the area. Folders called `readings/03-demand`
give topics ("3. Demand").

## Settings

Settings live in `~/.config/refs/config.toml`, edited on the settings page
(sidebar, or `,`) or by hand; comments you write there are kept. Every change
is checked before it is saved and applies at once. Besides the folders above:
the Obsidian vault, your annotation names, project `.bib` files, what to read
on its own, the monthly limits, the search model, and the API key (kept in
the macOS Keychain, never in a file).

## Costs and privacy

- To identify a PDF, refs sends the **text of its first three pages** (an image
  of page one for scans) to Claude Haiku, about $0.001 a paper, on your own API
  key. Searches that ask Claude send titles, authors, tags and summaries of
  about 40 candidates. Never file names, your notes, or anything in a folder
  marked never-sent-to-Claude.
- Registry lookups go to Crossref and OpenAlex (titles and identifiers).
- Reading on its own stops at a monthly limit ($10 by default); asking stops
  at $1. More than 25 new PDFs at once wait for your OK, with their cost, in
  the page's footer. `refs process` asks before a large run.
- The page is served on `127.0.0.1` only, and takes changes only from itself.
- refs renames files only in the quick-add folder or when you tick them under
  Review, never inside folders another tool keeps in sync or Git repositories,
  and keeps an undo journal. It never modifies Zotero.

## Command line

```sh
refs init                      # set refs up (the first-run questions)
refs doctor                    # check that everything is in order
refs scan                      # bring the index in line with your folders
refs status                    # what the library holds
refs find blp                  # search titles, authors, initials, file names, notes
refs find course:240a kind:paper topic:demand
refs find IO paper on ice cream   # a description works too (matched by meaning)
refs ask demand for frozen desserts   # let Claude pick from ~40 candidates (~$0.0005)
refs contexts                  # courses and projects recognised from folders
refs dupes                     # files stored in more than one place
refs open blp                  # open the best match
refs path blp                  # print its full path

refs add w32515                # add: NBER number, arXiv id, DOI, URL or a PDF file
refs add w32515 --queue        # ... and put it on the reading queue
refs queue                     # the reading queue, oldest first
refs wanted                    # papers saved without a PDF yet
refs bib blp                   # BibTeX for the best match (or `refs bib project:thesis`)
refs bib blp --append references.bib   # add it unless the file already has it
refs rename in:dump            # propose tidy names; add --apply to rename
refs rename --undo 3           # reverse the last 3 renames and moves

refs process --all             # read everything not yet read (asks first if many)
refs setup-key                 # store the Anthropic API key in the Keychain
refs skill install             # teach coding agents to use refs
refs zotero-import --dry-run   # what importing ~/Zotero would do
refs serve --open              # run the page in this terminal instead of the background
refs service status            # the background service (install, restart, uninstall)
```

`--json` on `find`, `status`, `contexts`, `dupes` and `scan` gives
machine-readable output.

## Troubleshooting and removal

`refs doctor` checks the settings, folders, index, API key, spending, notes,
search model and background service, and says how to fix what is wrong; its
report names no paper and never shows the key, so it is safe to send when
asking for help. The log is `~/Library/Logs/refs.log`.

`refs uninstall` lists, then removes, what refs made on your Mac: the
background service, the index, caches, log, settings and Keychain key
(`--keep-settings` and `--keep-key` keep those). Your PDFs, your notes and
refs' records in the library folder (`.refs/`, which a reinstall picks up
again) stay. Then `uv tool uninstall refs` removes the program.

## License

MIT. Working on refs itself: [docs/development.md](docs/development.md).
