# refs

A local-first, research-first reference manager for macOS that sits on top of the folders you already sync — Dropbox, iCloud Drive, Google Drive, OneDrive, or any local directory.

`refs` gives you one clean library folder for your PDFs, flexible project and course collections (including shared coauthor folders), registry-verified metadata, hybrid keyword + local semantic + LLM search, searchable PDF highlights synced to Obsidian, LaTeX/BibTeX workflows, and native support for coding agents (Claude Code, Codex).

- **Your files stay yours**: plain PDFs on disk, human-readable JSON records synced alongside them, a rebuildable local index, and a live `library.bib` export. Zero lock-in.
- **Research-first workspaces**: every project and course gets its own workspace with reading progress, status filters, BibTeX append, batch import, and shared coauthor folder sync.
- **Keyboard-driven local web UI** at `http://refs.localhost:8377`, paired with a fast CLI (`--json` ready).

`refs` runs on macOS. It was built by an economist for economists — its field/method vocabulary (`src/refs/llm.py`) and working-paper workflows reflect economics — though any academic PDF library works.

---

## Why `refs`?

Academic reference management usually forces an uncomfortable trade-off:

- **Traditional reference managers** (Zotero, Mendeley, Paperpile) trap PDFs inside opaque internal databases or fight with the tools you actually use: Dropbox/iCloud sync, Preview or iPad PDF annotation, Obsidian vaults, shared coauthor folders, and hand-maintained Overleaf `.bib` files.
- **Plain folders** give you full ownership and easy access across devices, but break down as soon as a paper belongs to two projects, filenames stay cryptic (`1-s2.0-S0014292123001234-main.pdf`), you lose track of what you read or highlighted, or you remember an idea (*"that IO paper on ice cream"*) rather than an author's name.

On top of that, everyday academic workflows have quirks that most managers ignore:
- Papers enter your library as NBER working papers, arXiv preprints, SSRN postings, or job market papers and get published in a journal years later.
- Publishers block automated PDF downloaders while your browser is already signed in through your university library proxy.
- Coauthors drop and annotate PDFs in shared Dropbox folders.
- Course folders and `~/Downloads` mix real papers with slides, problem sets, and receipts.
- Coding agents (Claude Code, Codex) increasingly help with literature reviews and LaTeX writing, and need trustworthy, structured access to *your* library and project citation keys.

`refs` is built around those realities.

---

## Design & Architecture Philosophy

### 1. One canonical library, many collections (research-first)
Every paper you care about lives **once** in a flat central library folder (your quick-add folder, e.g. `refs-library` or `papers-dump`) under a tidy canonical filename (`surname-year-short-title.pdf`). Research projects and courses are lightweight **collections** (with nested parts such as `219a/public-economics`), not competing folders. A paper can belong to any number of projects and courses without duplicating files.

Folders still play two explicit roles where they actually help:
- **Shared coauthor folders**: Marking a project collection as *shared* automatically keeps recorded copies in that Dropbox folder for your coauthors, watches for papers they add, and tracks annotations across copies.
- **Inboxes**: Course folders, Canvas/bCourses sync folders, and `~/Downloads` stay *outside* the main index so slides, problem sets, and receipts never clutter your library. Instead, `refs` scans them locally for paper-like PDFs and lets you pull readings into the library with course and topic pre-filled.

*(If your papers already live across nested course or project folders, `refs` also supports folder-based context rules — see [Folder rules](#folder-rules).)*

### 2. Zero lock-in: plain files, portable state, rebuildable cache
`refs` separates durable user state from disposable indexes:

| Layer | Location | Role |
|---|---|---|
| **Your PDFs** | Your library folder (Dropbox, iCloud, etc.) | Plain files you can open in Preview, annotate on an iPad, or browse in Finder. |
| **Synced records** | `<library>/.refs/` | Small, versioned JSON files per work (`works/`), read records, `collections.json`, undo journal, annotation backups, and a continuously updated `library.bib`. Syncs across Macs; older versions never overwrite newer schemas. |
| **Reading notes** | Your Obsidian vault (`<citekey>.md`) | Standard Markdown with YAML front matter. |
| **Local index** | `~/Library/Application Support/refs/index.db` | Fast SQLite FTS5 index, semantic vectors, cached highlights, and reading ledger — purely a rebuildable cache. |

If you stop using `refs` tomorrow, every PDF has a clean filename, every note is plain Markdown in Obsidian, and `library.bib` carries your full library with JabRef/Zotero-compatible `file` and `keywords` (collections) fields.

### 3. Content-addressed files vs. intellectual works
`refs` distinguishes a **file** (identified by its SHA-256 content hash and disk inode) from a **work** (one intellectual paper with a permanent, immutable citekey).
- Move, rename, or highlight a PDF — even offline or on an iPad — and `refs` reconciles it by inode or hash without losing its metadata, collections, notes, or reading history.
- Multiple copies (e.g., your library copy and a shared coauthor copy, or a working paper alongside its published article) attach cleanly to the same work.

### 4. The reliability rule: *the model never authors a bibliographic fact*
LLMs are great at reading messy PDF title pages and summarizing papers, and terrible at hallucinating DOIs or page numbers. `refs` enforces a strict division of labor:
1. **Local extraction** reads the text of the first three pages (or an image of page 1 for scans) and any printed identifiers (DOI, arXiv, NBER, SSRN, JSTOR).
2. **Claude Haiku** transcribes what is printed, classifies the document kind, and writes a short summary and field/method tags. It is deliberately blind to the filename so the filename remains an independent check.
3. **Crossref & OpenAlex** supply the authoritative bibliographic record, accepted only if the title is at least 90% similar and at least half the printed authors match, selecting the right version by year.
4. **Explicit provenance**: Every paper carries a visible status mark (`■ verified`, `▣ confirmed`, `◧ extracted`, `! needs review`) so you always know how much to trust a citation before putting it in a draft.

### 5. Local-first, fast, and scriptable
The web interface is served strictly on `127.0.0.1` (`http://refs.localhost:8377`) using plain HTML, CSS, and modular ES modules with no frontend build step, full keyboard navigation, and URL hash routing (Back/Forward, reload persistence, and deep links to any work or collection). Everything is equally accessible from the `refs` CLI with `--json` output for scripts and coding agents.

---

## Key Features

### Project & course workspaces
Open any `#project` or `#course` from the sidebar to enter its dedicated workspace:
- **Progress & quick filters**: See reading progress at a glance (`read / skimmed / queued`) and slice the collection with one-click pills (`all`, `queued`, `unread`, `reading`, `skimmed`, `read`, `no pdf`).
- **Workspace actions**: `+ add to #name`, `import list/.bib`, `append to .bib`, `copy BibTeX`, `open folder`, and `open next 5 via proxy` (to quickly grab paywalled stubs through your university library proxy).
- **Flexible hierarchy & sidebar**: Nest sub-collections with slashes (`219a/public-economics`, automatically included in `219a`). Pin favorites to *Pinned*, drag to reorder projects, sort sections (custom order, A–Z, newest, recently used, size, or term), fold or hide sections and old terms, and act on anything via right-click menus (or `.` on the highlighted paper). Pinning, ordering, and hidden state sync via `collections.json`.

### Capture: 1-click PDF bookmarklet, quick add & resilient batch import
- **1-click PDF bookmarklet** (`/bookmarklet` → drag **+ refs** to your bookmarks bar): Works on both open PDFs (native browser PDFs, publisher viewers like Wiley/Econometrica, Chicago/JPE, T&F, AEA, OUP, ScienceDirect, SSRN, arXiv, NBER, signed CDNs, and library-proxy sessions) and article pages. It reads the PDF straight from your browser's own authenticated session/cache and uploads it directly into your library folder — no downloading to `~/Downloads`, no hitting Enter, resilient to COOP/CSP/popup blockers. On a paywalled article page with a **library proxy** configured in settings (e.g., `https://libproxy.berkeley.edu/login?qurl=`), it reopens the page through your proxy. A sticky *into* field remembers your target collection during a literature review.
- **Quick add & drag-and-drop**: Press `a` (or run `refs add`) and paste a URL, DOI, arXiv ID, or NBER number; add `#collection` (Tab-completed, current term's courses first) to file immediately, or press `Shift+Enter` to also queue it. Drag PDFs onto the page (or drop them into your library folder from Finder/iPad/Dropbox) and the watcher picks them up automatically. Paywalled papers are saved as clean `no pdf` stubs that attach automatically once the PDF arrives.
- **Resilient 3-stage batch importer** (*batch import* in the sidebar): Paste or drop a `.bib` file, a list of DOIs/URLs/arXiv/NBER IDs, or a plain-text syllabus reading list (with optional Claude parsing for messy syllabi):
  1. **Parse** entries and section/week headings (into sub-collections).
  2. **Preview & reconcile** against your existing library and Crossref/OpenAlex first — papers you already own are matched and simply tagged into the target collection (zero duplicates).
  3. **Commit** immediately with verified metadata: reliable open-access PDFs (arXiv, NBER, OpenAlex OA, direct PDFs) download in the background, while paywalled papers enter as clean `no pdf` stubs ready for batch proxy capture.
- **Downloads & Course Inboxes** (*Review → downloads / inbox*): Scans `~/Downloads`, unindexed course folders, and course-site sync folders locally (for free) to separate real papers from receipts, tickets, and slides. Pull selected papers straight into the library with course/topic pre-filled (moving from `~/Downloads` or copying out of managed sync folders), and safely **trash** `~/Downloads` copies whose exact bytes are already in `refs`.

### 3-tier hybrid search
Search starts instant and free, escalating to Claude only when needed:

| Tier | What it searches | Cost |
|---|---|---|
| **Words (FTS5)** | Prefix-matches every word across title, authors, initials (`blp`), citekeys, project `.bib` keys, filenames, personal & auto tags, summaries, Obsidian notes, and **extracted PDF highlights**. Economics abbreviations (`IO`, `PF`, `RDD`, …) expand to their spelled-out fields. | Free |
| **Meaning (local)** | On-device embedding model (`fastembed`) matches descriptions against titles, tags, summaries, notes, and highlights — so *"frozen desserts"* finds an ice-cream demand paper. | Free |
| **Claude Haiku rerank** | Ranks ~40 local candidates and explains *why* each fits. Triggered only when you press Enter on a natural-language description and local results look unsure, or explicitly via `?` / `refs ask`. | ~$0.0005 (capped at \$1/mo) |

Combine free text with structured filters: `#collection` (or `collection:`), `course:`, `project:`, `term:`, `area:`, `in:`, `topic:`, `kind:`, `year:`, `author:`, `tag:`, `status:`, `read:` (`unread`, `reading`, `skimmed`, `read`), `queued:` (`yes`, `no`).

### Reading status, reading log, queue & searchable PDF highlights
- **4-state reading cycle** (`r`): `unread` (`○`) → `reading` (`◐`) → `skimmed` (`◔`) → `read` (`●`). Manual choices always win; otherwise `refs` detects reading progress automatically from your own PDF highlights, underlines, notes, and ink (marks on ≥30% of pages and ≥5 pages → *read*; ≥2 pages → *reading*; books >150 pages cap at *reading*). Configure your annotation author names under Settings (*"find names in my PDFs"*) so an instructor's or coauthor's markup never counts as yours.
- **Reading log**: Record multiple reads or re-reads on any paper with date, depth (`read`, `skimmed`, `reading`), project/course context, and a 1-line takeaway.
- **Reading queue with intent**: Press `q` to toggle the queue, or `Q` to queue with a 1-line reason (*"why did I queue this?"*). Tie queued papers to a collection (`--for`), pin top priorities to **★ up next**, and let papers leave the queue automatically once read.
- **Standalone searchable PDF highlights**: `refs` extracts the actual text you highlighted and the margin comments you wrote (cached per PDF content hash), indexes them into both keyword and semantic search, and displays them with page numbers and highlight colors in the detail pane (`refs highlights <query>`).

### Deep Obsidian integration
- **Stable citekey notes (`n`)**: One Markdown file per work (`<vault>/<folder>/<citekey>.md`), so `[[citekey]]` links cleanly from anywhere in your vault. Notes include an `Author (Year)` alias for Obsidian autocomplete.
- **Safe concurrent editing**: Edit notes directly in `refs`; saves check a byte hash of the file first and write atomically via a temporary file so concurrent edits in Obsidian are never overwritten.
- **Idempotent highlight pull**: Click **pull into note** (or run `refs highlights <query> --to-note`) to insert or refresh a delimited `<!-- refs:highlights:start --> … <!-- refs:highlights:end -->` Markdown block without touching your own writing.
- **Managed front-matter sync**: Keeps `refs`-managed YAML properties (`title`, `authors`, `year`, `venue`, `doi`, `citekey`, `collections`, `read_status`, `aliases`) up to date while preserving every custom property and comment you add in Obsidian.
- **Vault backlinks**: The detail pane lists every other note in your Obsidian vault that links to `[[citekey]]`, with one-click `obsidian://` links.

### Shared coauthor folders & annotation sync
- **Shared project collections** (`--shared`, shown with `⇄`): Adding a paper copies its PDF into the shared Dropbox folder for coauthors; PDFs a coauthor adds surface in **Inbox** ready to copy into your library; removing a paper from the collection asks before trashing the shared copy. Recorded library + shared copies stay out of the Copies review.
- **Per-copy annotation tracking & pull-merge**: When a work has multiple copies, `refs` shows whose marks each copy holds and when it was last annotated, flagging `⚑` any copy that has annotations your library copy lacks. Click **Bring them into the library copy** (or run `refs marks <query> --pull '#project'`) to redraw those highlights and comments into your library copy (preserving author, text, and color, after saving a backup of the previous library PDF in `.refs/`).

### Working-paper upgrades & library maintenance (`Review`)
- **Working paper → published checker** (*Review → published* / `refs updates --check`): Re-queries Crossref and OpenAlex for your NBER, arXiv, SSRN, and draft papers (bypassing old cached misses) and surfaces verified journal publications. Accepting upgrades venue, year, volume, issue, pages, and DOI while keeping your citekey, collections, reading log, notes, PDF, and original working-paper provenance intact.
- **Needs review, PDF re-read, merge & tags**: Fix metadata or fetch by DOI, re-read a single paper or a multi-selected batch with Claude and fresh registry lookups while preserving all user state, merge duplicate works (combining files, collections, and notes under the target citekey), add personal tags, and hide noisy auto-tags (restorable anytime).
- **Copies & renames**: *Copies* spots identical files, marked-up vs. clean copies, and working papers next to published versions, recommending which copy to keep (weighing your marks first, then version, location, and name) with one-click cleanup for safe sets. *Renames* proposes tidy canonical filenames (`surname-year-short-title.pdf`) with an undo journal (`u` / `refs rename --undo`).

### BibTeX, LaTeX & project `.bib` interoperability
- **Clean biblatex/biber output**: UTF-8 entries with automatic acronym and mixed-case bracing (`{US}`, `{GDP}`, `{OLS}`, `{COVID-19}`, `{R\&D}`) so sentence-casing styles never mangle titles.
- **Project `.bib` awareness**: Point `refs` at your project `.bib` files (read-only, per collection or in settings) and it learns the citation keys those projects already use — so `refs find o2006optimal` finds `odonoghue2006optimal`, and pressing `y` inside a project copies that project's key.
- **Duplicate-free append**: `refs bib '#bets' --append` (or *append to .bib* in the workspace) appends entries to a `.bib` file while skipping any paper already present under *any* key (matched by key, DOI, or title).
- **Live `library.bib`**: Continuously maintained in `.refs/library.bib` with relative `file` paths and `keywords` for JabRef and Zotero portability.

### Coding agents (Claude Code & Codex)
Run `refs skill install` to install the bundled agent skill (`src/refs/skill/SKILL.md`). Coding agents learn to search your library first (`refs find --json`), inspect collections and reading status, resolve `\cite{key}` references from `.tex` files, fetch verified BibTeX, queue or save papers, and leave confidential (`never_sent_to_ai`) folders and destructive file actions untouched.

---

## Record Status Reference

Every paper in the list shows a status mark indicating how far to trust its bibliographic metadata:

| Mark | Status | Meaning |
|---|---|---|
| `■` | verified | A Crossref or OpenAlex record agrees with what is printed in the PDF |
| `▣` | confirmed | You accepted, edited, or upgraded the record |
| `◧` | extracted | No registry record; Claude's transcription of the opening pages (drafts, JMPs) |
| `!` | needs review | Something did not match cleanly; inspect under *Review → needs review* |
| `·` | classified | Non-library item (slides, problem set, syllabus, etc.) |
| `·` | confidential | In a `never sent to Claude` folder; read from printed identifiers only |
| `◆` | imported | Imported from Zotero and not yet read |
| `□` | no file | Metadata stub saved without a PDF yet |

---

## Install & First Run

You need [uv](https://docs.astral.sh/uv/) (`brew install uv`) and an [Anthropic API key](https://console.anthropic.com/settings/keys). Obsidian is optional.

```sh
uv tool install git+https://github.com/williamradaic/refs
refs service install
```

`refs service install` installs a macOS login agent that runs `refs` in the background (starting at login) and opens `http://refs.localhost:8377`. To update later:

```sh
uv tool upgrade refs
refs service restart
```

### Onboarding wizard
On first run, the page walks you through six steps (or run `refs init` in the terminal):
1. **Library root**: Where your synced folders live (Dropbox, iCloud Drive, Google Drive, OneDrive, Box, Documents, or any custom folder).
2. **Folders with papers**: A folder tree with PDF counts; tick the folders `refs` should watch. Term folders (`Fall 2026`, `2025-spring`) are detected automatically.
3. **Quick-add / library folder**: Where newly added papers are stored and tidily named (created if new).
4. **Private folders**: Exclude personal paperwork completely (`visa`, `taxes`, …) or mark confidential folders (`referee`, …) to index locally without ever sending text to Claude.
5. **Notes**: Select an Obsidian vault for reading notes, or leave notes off.
6. **Reading**: Store your Anthropic API key (in the macOS Keychain) and choose whether to identify all existing PDFs now (with a cost estimate), only new papers added from now on, or none.

Everything can be changed later under **settings** (`,`). Run `refs doctor` anytime to verify every component.

### Everyday keyboard shortcuts

| Key | Action | Key | Action |
|---|---|---|---|
| `j` / `k` | Move down / up (`g` / `G` top / bottom) | `r` | Cycle reading status (`unread → reading → skimmed → read`) |
| `/` | Focus search (`Esc` clears) | `q` / `Q` | Toggle queue / queue with a 1-line reason |
| `⏎` / `o` | Open PDF in browser tab (`p` Preview, `f` Finder) | `n` | Edit reading note (Obsidian) |
| `a` | Add a paper (`Shift+Enter` also queues) | `y` / `b` | Copy citekey (project key first) / copy BibTeX |
| `#` | Add paper or selection to a collection | `x` / `⌘A` | Multi-select rows (selection persists across searches) |
| `F` / `C` / `u` | File to folder / Copy to folder / Undo | `.` | Context menu for paper or selection |
| `,` / `w` / `R` | Settings / watcher status / rescan | `?` | Full shortcut reference (`t` toggles light/dark) |

**Tips**: `/?q=<search>` opens directly to a search; `/?add=<link>` pre-fills the add bar; URL hashes (`#c=bets&w=...`) support browser Back/Forward and deep links. In Chrome, *⋮ → Cast, Save and Share → Install page as app* gives `refs` its own standalone window. Raycast users can import [extras/raycast-quicklinks.json](extras/raycast-quicklinks.json).

---

## Settings, Folder Rules, Costs & Privacy

### Settings
Settings live in `~/.config/refs/config.toml` and can be edited on the **settings** page (`,`) or by hand (comments are preserved). Every change is validated before saving and applies immediately.

#### Folder rules
If you keep papers in existing course or project folders, `refs` also derives implicit collections from folder paths, tried in order:

| Setting | What it does |
|---|---|
| `contexts` | Custom rules, e.g. `Classes/{term}/{course}`, `Research/{name}` (`{term}` matches `2026-fall`, `Fall 2026`; `{course}` normalizes `1-Econ 240A` → `240a`; `{name}` / `{area}` match any folder). Subfolders like `readings/03-demand` become topics. |
| `layout = "phd"` | Built-in preset for `phd/coursework/<term>/<course>`, `phd/research/<project>`, `phd/referee/<journal>/<manuscript>`, etc. |
| `material_dirs` | Folder names treated as course material (slides, handouts) rather than papers. |

### Costs & guardrails
- **Reading new PDFs**: Sends only the text of the first three pages (an image of page 1 for scans) to Claude Haiku — roughly **$0.001 per paper**, billed to your own API key.
- **AI search reranking**: Sends titles, authors, tags, and summaries of ~40 local candidates — roughly **$0.0005 per query**, cached locally.
- **Spend caps & batch approval**: Automatic reading pauses at a monthly cap (**\$10/month** by default); AI search caps at **\$1/month**. Batches of more than 25 new PDFs wait for your confirmation (with an upfront cost estimate) before spending anything.

### Privacy & safety
- **Never sent to AI**: Filenames, your Obsidian notes, anything in excluded folders, and anything inside folders marked `no_llm` (*never sent to Claude*).
- **Registry lookups**: Send only titles and identifiers to Crossref and OpenAlex.
- **Credentials & network**: Your API key is stored in the **macOS Keychain** (never in a config file). The web server binds exclusively to `127.0.0.1` and uses a per-Mac token for the bookmarklet.
- **Non-destructive file handling**: `refs` only renames files automatically in your quick-add/library folder or when you explicitly approve renames under Review; it never renames files inside Git repositories or managed sync folders (like Overleaf), records every move/copy/rename in an undo journal, and never modifies Zotero.

---

## Command Line

```sh
# Setup & health
refs init                                  # interactive first-run setup
refs setup-key                             # store Anthropic API key in Keychain
refs doctor                                # check setup, folders, index, key, service
refs service status                        # background service (install, restart, uninstall)
refs serve --open                          # run in the foreground instead of background
refs skill install                         # teach Claude Code & Codex to use refs

# Searching & browsing
refs scan                                  # reconcile index with watched folders
refs status                                # library summary and totals
refs find blp                              # keyword + local semantic search
refs find '#220a' kind:paper read:unread   # filter by collection, kind, reading state, ...
refs ask IO paper on ice cream             # Claude Haiku rerank (~$0.0005)
refs open blp                              # open best match in default PDF viewer
refs path blp                              # print absolute path of best match

# Capturing, reading & highlights
refs add w32515 --to bets --queue          # add NBER/arXiv/DOI/URL/PDF into collection & queue
refs queue                                 # list reading queue (use `refs queue blp --next --note "..."`)
refs wanted                                # papers saved as stubs without a PDF yet
refs highlights blp --to-note              # extract PDF highlights (and pull into Obsidian note)
refs updates --check                       # check working papers for published journal versions

# Projects, courses & shared folders
refs project new bets --folder phd/research/bets/readings --shared --bib ~/phd/research/bets/refs.bib
refs project new 220a --course --term 2026-fall
refs collection                            # list collections (add, remove, rename, new, delete)
refs collection add bets o'donoghue        # add best match (--all for every match)
refs marks blp --pull '#bets'              # pull annotations from a shared copy into library copy

# BibTeX & maintenance
refs bib blp                               # print BibTeX (or `refs bib '#bets'`)
refs bib '#bets' --append                  # append to the collection's .bib (deduplicated)
refs bib --library                         # regenerate .refs/library.bib now
refs rename in:dump                        # propose tidy names (--apply to rename; --undo N to revert)
refs dupes                                 # list duplicate file locations
refs process --all                         # identify unread PDFs (--redo / --relookup)
refs zotero-import --dry-run               # preview importing a ~/Zotero library
```

Add `--json` to `find`, `ask`, `status`, `contexts`, `collection`, `dupes`, `scan`, `queue`, `highlights`, and `updates` for machine-readable output.

---

## Troubleshooting & Removal

- Run `refs doctor` to diagnose settings, folders, index, Keychain key, budget, Obsidian vault, local search model, and background service. Its output never names a paper or prints your API key, so it is safe to share when reporting an issue.
- Background service logs live at `~/Library/Logs/refs.log`.
- To remove `refs` from your Mac:
  ```sh
  refs uninstall               # removes service, index, caches, log, settings, Keychain key
  uv tool uninstall refs       # removes the CLI binary
  ```
  Pass `--keep-settings` or `--keep-key` to preserve those. Your PDFs, Obsidian notes, and `.refs/` records inside your library folder are always left untouched so a reinstall picks up where you left off.

---

## Development & License

MIT License — see [LICENSE](LICENSE).

- Contributing, code map, and local sandbox servers: [docs/development.md](docs/development.md)
- Research-first architecture notes: [docs/research-first-plan.md](docs/research-first-plan.md)
