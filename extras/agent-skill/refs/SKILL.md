---
name: refs
description: Look up William's own library of papers with the local `refs` command, which indexes every PDF in William's Dropbox PhD folders (course readings, research literature, papers-dump). Use it whenever a task involves a paper, author, citation or reading list: finding a paper or its PDF, checking whether William already has something, getting a BibTeX entry or citekey, adding \cite entries to a project's .bib, resolving a \cite key in a .tex file, listing a course's readings, or saving a new paper (URL, DOI, arXiv id, NBER number) to read later. Prefer it over web search for papers William may already have, even when refs is not mentioned by name.
---

# refs: William's paper library

`refs` is a command-line reference manager on this Mac (source: `~/phd/tools/refs`,
README there). It indexes the PDFs under `Dropbox/phd` and `Dropbox/papers-dump`,
knows which course or project each one belongs to from its folder, and holds
metadata checked against Crossref and OpenAlex. A web view runs at
http://refs.localhost:8377 (`/?q=<search>` opens a search).

Look things up here first: it answers from William's own files, with registry-checked
metadata, which is better than a web search for anything already in the library.

## Finding papers

```sh
refs find <words> [filters] --json          # best matches first; default limit 50
refs find berry levinsohn pakes --json --limit 3
refs find course:220a kind:paper --json --all
```

Words match title, authors, author initials ("blp"), citekeys, file names, tags,
descriptions and William's reading notes, as prefixes. A query of several words
is also matched by meaning (a local embedding model, free), so a description
works: `refs find paper on misperceived nonlinear tax schedules --json`. Field
abbreviations such as `IO`, `PF`, `RDD` match their spelled-out tags. Filters: `course:` / `project:` (folder slug, e.g.
`220a`, `bets`), `term:` (`2026-fall`), `area:` (`coursework`, `research`,
`dump`, ...), `in:` (context contains), `topic:`, `kind:` (`paper`, `book`,
`chapter`, `slides`, `syllabus`, `notes`, `problem-set`, ...), `year:`,
`author:`, `tag:`, `status:`.

Each JSON result has `title`, `authors` (surnames), `year`, `venue`, `doi`,
`citekey`, `status`, `kind`, `tags`, `description` (a short summary written from
the paper itself) and `locations` (Dropbox-relative `path`, plus `context`,
`topic`). Library works have a `citekey`; course material such as slides has
none.

`status` says how far to trust the metadata: `verified` (a registry record
matches the PDF) and `confirmed` (William checked it) are reliable; `extracted`
is the model's transcription of the first page (fine for drafts and job market
papers, but say so when it matters); `needs-review` means something did not add
up, so double-check before citing.

`refs find --json` never calls a model. When a description finds nothing
convincing, `refs ask <description> --json` has Claude Haiku rank a shortlist of
about 40 local candidates (about $0.0005 a question; answers are cached and
capped at $1 a month). Its results carry a `why` field; an empty list means
Haiku judged that nothing in the library fits. Use it sparingly, after `find`.

Other read-only commands:

```sh
refs path <query>       # absolute path of the best match (to read the PDF)
refs open <query>       # open it in the default PDF viewer (only when asked to show it)
refs contexts --json    # the courses and projects refs knows
refs wanted             # papers saved without a PDF yet
refs status             # library totals
```

## Citations and BibTeX

```sh
refs bib <query>                       # BibTeX of the best match
refs bib <query> --limit 5             # several matches
refs bib project:bets                  # every work filed under a project
refs bib <query> --append path/to/references.bib
```

Keys look like `berry1995automobile` (surname, year, first title word). Entries
are written for biblatex with biber and UTF-8.

`--append` adds entries to the end of a .bib file and skips any paper already in
it under any key (matched by key, DOI or title); it prints `present @key` for
those, which is also how to learn which key a project already uses for a paper.
Use the key that file already has rather than introducing a second one. Append
only when William asks for entries in a file, since the .bib is William's. A .bib under
an `overleaf/` folder is its own Git repository that is also edited on the
Overleaf website: run `git pull` there before appending, and leave committing
and pushing to William unless asked.

To resolve a `\cite{key}` from a .tex file, run `refs find <key> --json`: refs
knows its own keys, keys imported from Zotero, and the keys William's project
.bib files use for library papers (research, RA and project repositories). A
key that is not found is usually a paper that is not in the library; read its
entry from the project's .bib, and search by title words or `author:` and
`year:` to be sure.

## Adding a paper

```sh
refs add <URL | DOI | arXiv id | NBER number | path/to/file.pdf>
```

This downloads the PDF when it is openly available into `Dropbox/papers-dump`,
reads it with Claude Haiku (about $0.001 a paper) and looks it up in the
registries; a paywalled paper is saved without a PDF and attaches itself when
the PDF later lands in papers-dump. Run it when asked to save or add a paper. A PDF saved into papers-dump by any other means is picked up by the
running refs service on its own, so there is no need to call `add` for it.

## Leave these to William

These change William's files or spend money in bulk, so run them only when
asked for exactly that: `refs rename ... --apply` and `refs rename --undo`,
`refs process` (reads many PDFs through the API), `refs zotero-import`,
`refs service ...` and `refs setup-key`. Never move, rename or delete PDFs in the
Dropbox folders yourself; refs keeps an undo journal for the changes it makes,
and the web page has Review views for renames, copies and filing papers.

Files under `phd/referee` are confidential manuscripts: refs indexes them but
never sends their text to a model, and neither should you.
