---
name: refs
description: Look up the user's own library of papers with the local `refs` command, which indexes the PDFs in their folders (course readings, research literature, a quick-add folder) and knows which course or project each belongs to. Use it whenever a task involves a paper, author, citation or reading list: finding a paper or its PDF, checking whether the user already has something, getting a BibTeX entry or citekey, adding \cite entries to a project's .bib, resolving a \cite key in a .tex file, listing a course's readings, or saving a new paper (URL, DOI, arXiv id, NBER number) to read later. Prefer it over web search for papers the user may already have, even when refs is not mentioned by name.
---

# refs: the user's paper library

`refs` is a command-line reference manager on this Mac. It indexes the PDFs in
the folders the user chose (inside their Dropbox, iCloud Drive or another
library folder), knows which course or project each one belongs to from its
folders, and holds metadata checked against Crossref and OpenAlex. A web view
runs at http://refs.localhost:8377 (`/?q=<search>` opens a search).

Look things up here first: it answers from the user's own files, with
registry-checked metadata, which is better than a web search for anything
already in the library.

To learn this library: `refs status --json` gives the library folder, the
watched folders (`roots`), the quick-add folder (`dump`), the folders whose
text must never go to an AI (`never_sent_to_ai`) and totals; `refs collection
list --json` lists the courses and projects (collections). If `refs` says it is not set up, tell
the user to run `refs init` (or open the page); do not set it up for them.

## Finding papers

```sh
refs find <words> [filters] --json          # best matches first; default limit 50
refs find berry levinsohn pakes --json --limit 3
refs find collection:<name> kind:paper --json --all
```

Words match title, authors, author initials ("blp"), citekeys, file names,
tags, descriptions and the user's reading notes, as prefixes. A query of
several words is also matched by meaning (a local embedding model, free), so a
description works: `refs find paper on misperceived nonlinear tax schedules
--json`. Field abbreviations such as `IO`, `PF`, `RDD` match their spelled-out
tags. Filters: `collection:<name>` (also written `#<name>`, quoted in a shell:
a course or project; `219a` includes its parts such as `219a/public-economics`),
`course:` / `project:` (a collection, or a folder name), `term:` (`2026-fall`),
`area:`, `in:` (context contains), `topic:`,
`kind:` (`paper`, `book`, `chapter`, `slides`, `syllabus`, `notes`,
`problem-set`, ...), `year:`, `author:`, `tag:`, `status:`, `read:` (`read`,
`reading`, `unread`), `queued:yes`.

Each JSON result has `title`, `authors` (surnames), `year`, `venue`, `doi`,
`citekey`, `status`, `kind`, `tags`, `description` (a short summary written
from the paper itself), `read_status` (set by the user, or detected from their
highlights in the PDF), `queued` (when they put it on their reading queue, or
null), `collections` (the courses and projects it belongs to) and `locations`
(`path` relative to the library folder, plus `context` and `topic`). Library works have a `citekey`; course material such as slides
has none.

`status` says how far to trust the metadata: `verified` (a registry record
matches the PDF) and `confirmed` (the user checked it) are reliable;
`extracted` is the model's transcription of the first page (fine for drafts
and job market papers, but say so when it matters); `needs-review` means
something did not add up, so double-check before citing.

`refs find --json` never calls a model. When a description finds nothing
convincing, `refs ask <description> --json` has Claude Haiku rank a shortlist
of about 40 local candidates (about $0.0005 a question, on the user's API key;
answers are cached and capped each month). Its results carry a `why` field; an
empty list means nothing in the library fits. Use it sparingly, after `find`.

Other read-only commands:

```sh
refs path <query>       # absolute path of the best match (to read the PDF)
refs open <query>       # open it in the default PDF viewer (only when asked to show it)
refs collection list --json   # collections: name, kind, term, folder, shared, bib_file, works
refs contexts --json    # the folders refs reads courses and projects from
refs wanted             # papers saved without a PDF yet
refs status             # the library's folders and totals
refs doctor --offline   # whether refs is set up and working
```

## Citations and BibTeX

```sh
refs bib <query>                       # BibTeX of the best match
refs bib <query> --limit 5             # several matches
refs bib collection:<name>             # every work in a collection (course or project)
refs bib collection:<name> --append    # into the collection's own .bib file, if it has one
refs bib <query> --append path/to/references.bib
```

Keys look like `berry1995automobile` (surname, year, first title word).
Entries are written for biblatex with biber and UTF-8.

`--append` adds entries to the end of a .bib file and skips any paper already
in it under any key (matched by key, DOI or title); it prints `present @key`
for those, which is also how to learn which key a project already uses for a
paper. Use the key that file already has rather than introducing a second one.
Append only when the user asks for entries in a file, since the .bib is
theirs. A .bib under an `overleaf/` folder may be its own Git repository that
is also edited on the Overleaf website: run `git pull` there before appending,
and leave committing and pushing to the user unless asked.

To resolve a `\cite{key}` from a .tex file, run `refs find <key> --json`: refs
knows its own keys, keys imported from Zotero, and the keys the user's project
.bib files use for library papers (the `bib_files` setting). A key that is not
found is usually a paper that is not in the library; read its entry from the
project's .bib, and search by title words or `author:` and `year:` to be sure.

## Adding a paper

```sh
refs add <URL | DOI | arXiv id | NBER number | path/to/file.pdf>
```

This downloads the PDF when it is openly available into the quick-add folder,
reads it with Claude Haiku (about $0.001 a paper, on the user's API key) and
looks it up in the registries; a paywalled paper is saved without a PDF and
attaches itself when the PDF later lands in the quick-add folder. Run it when
asked to save or add a paper. A PDF saved into the quick-add folder by any
other means is picked up by the running refs service on its own, so there is
no need to call `add` for it.

## Collections

A paper belongs to any number of collections (courses and projects), whatever
folder its PDF is in. Put a paper in one when the user asks (for instance "add
this to my bets reading list"):

```sh
refs collection add <name> <search>      # the best match; --all for every match
refs collection remove <name> <search>
refs project new <name> [--course --term 2026-fall] [--folder F] [--shared] [--bib F]
```

A new name makes a new collection, so check the existing names first
(`refs collection list --json`) rather than inventing a near-duplicate.
`refs add <link> --to <name>` adds a paper straight into a collection. A
paper's first location is its library copy: read and cite that one. Putting a
paper in a *shared* collection copies its PDF into a folder coauthors see;
do that only when asked.
`library.bib` in refs' folder holds every paper with its PDF and collections;
it is for the user's other reference managers, not for citing (use `refs bib`).

## The reading queue

`refs queue` lists the papers the user means to read next (`--json` for
scripts). When they ask to save a paper *to read*, add it with
`refs add <link> --queue`; `refs queue <search>` queues a paper already in the
library and `refs queue --remove <search>` takes it off. Papers leave the
queue by themselves once read.

## Leave these to the user

These change the user's files, settings or spend money in bulk, so run them
only when asked for exactly that: `refs rename ... --apply` and
`refs rename --undo`, `refs process` (reads many PDFs through the API), `refs migrate --apply`,
`--undo` and `--finish`, `refs marks --pull`,
`refs zotero-import`, `refs init`, `refs service ...`, `refs setup-key` and
`refs uninstall`. Never move, rename or delete PDFs in the library folders
yourself; refs keeps an undo journal for the changes it makes, and the web
page has Review views for renames, copies and filing papers.

Folders listed in `never_sent_to_ai` (`refs status --json`) hold confidential
documents: refs indexes them but never sends their text to a model, and
neither should you.
