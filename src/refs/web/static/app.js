// refs: the page's behaviour. No framework: the state is one object, and each
// change re-renders the pane it affects.

const state = {
  query: "",          // what is typed in the search bar
  view: "all",        // sidebar preset: all | queue | unread | reading | read | review | wanted | renames | copies | downloads
  context: null,      // selected folder key, e.g. "dump" or "referee/econometrica"
  collection: null,   // selected collection (course or project), e.g. "220a" or "219a/public-economics"
  tag: null,          // selected field tag
  scope: "library",   // "library" = papers only, "all" = course material too
  items: [],          // current search results
  selected: 0,        // index of the highlighted row
  adding: false,      // true while the bar is in "add a paper" mode
  picked: new Set(),  // renames view: paths ticked for renaming; downloads view: file names ticked
  dlOthers: false,    // downloads view: also list the PDFs that do not look like papers
  dlMode: "move",     // downloads view: "move" out of Downloads, or "copy" (the original stays)
  dlQueue: false,     // downloads view: also put what is taken in on the reading queue
  marked: new Map(),  // rows selected for a batch action (⌘-click, ⇧-click, x): id → item.
                      //   Kept across searches, so a selection can be gathered from several.
  lastFiled: 0,       // how many files the last file to… / copy to… moved (u undoes them)
  lastBatch: 0,       // how many renames the last "rename selected" did (for undo)
  dump: "papers-dump", // the quick-add folder's name (config.toml `dump`), from the sidebar
};

const $ = (id) => document.getElementById(id);
// Order of terms, oldest to newest: "2026-fall" -> 2026*4 + 3. Terms arrive
// in one form (contexts.term_of). No term sorts before every term.
const SEASONS = { winter: 0, spring: 1, summer: 2, fall: 3 };
const termRank = (term) => {
  const [year, season] = String(term ?? "").split("-");
  return term ? Number(year) * 4 + (SEASONS[season] ?? 0) : Infinity;
};
const esc = (text) => String(text ?? "").replace(/[&<>"]/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
// Record status: squares (filled = confirmed by a registry, framed = by you,
// half = transcribed only, empty = no PDF). Circles belong to your reading.
const GLYPH = { verified: "■", confirmed: "▣", imported: "◆", extracted: "◧",
  "needs-review": "!", "no-file": "□", classified: "·" };

// --- columns ---------------------------------------------------------------------
// Each column knows its header label, default width (in characters; null = takes
// the remaining space), how to sort, and what to show.
const last = "￿";      // sorts after every real string: empty values go to the end
// Sort orders for the two glyph columns, as text so every key compares alike.
// Reading: the queue (in queue order), then started, read, unread.
const READ_RANK = { reading: "1", read: "2", unread: "3" };
// Status: from the most trustworthy record to the least, then no PDF, not a paper.
const STATUS_RANK = { confirmed: 0, verified: 1, imported: 2, extracted: 3, "needs-review": 4,
  "no-file": 5, confidential: 6, classified: 7 };
const COLUMNS = {
  // Your reading: ▸ on the queue, ◐ started, ● read (set by you or detected
  // from your markup). Circles, so they never look like the status column's
  // squares. The queue mark wins: a paper queued to re-read shows ▸.
  rd:      { label: "◐",       width: 1,  fixed: true, align: "center",
             sort: (i) => (i.queued ? "0" + i.queued : READ_RANK[i.read_status] ?? "9"),
             cell: (i) => {
               const status = i.read_status;
               const how = i.read_source === "detected" ? " (from your markup)" : "";
               if (i.queued) return `<span class="rd q" title="queued${status !== "unread" ? `, ${status}` : ""}">▸</span>`;
               if (status === "reading") return `<span class="rd" title="started${how}">◐</span>`;
               if (status === "read") return `<span class="rd done" title="read${how}">●</span>`;
               return `<span class="rd"></span>`; } },
  st:      { label: "■",       width: 1,  fixed: true, align: "center",
             sort: (i) => STATUS_RANK[i.status] ?? 8,
             cell: (i) => { const st = i.status ?? "classified";
               return `<span class="st ${st}" title="${st}">${GLYPH[st] ?? "·"}</span>`; } },
  authors: { label: "authors", width: 20, cls: "au", sort: (i) => (i.authors?.[0] ?? last).toLowerCase(),
             text: (i) => authorsShort(i.authors) },
  year:    { label: "year",    width: 5,  cls: "yr", align: "right", sort: (i) => i.year ?? 0, text: (i) => i.year ?? "" },
  title:   { label: "title",   width: null, cls: "ti", sort: (i) => (i.title ?? last).toLowerCase(),
             text: (i) => i.title ?? i.locations[0]?.path.split("/").pop() },
  abbr:    { label: "jrnl",    width: 9,  cls: "ab", sort: (i) => (i.abbr ?? last).toLowerCase(),
             text: (i) => i.abbr ?? "" },
  venue:   { label: "publication", name: "publication", width: 22, cls: "ve",
             sort: (i) => (i.venue_label ?? last).toLowerCase(), text: (i) => i.venue_label ?? "" },
  // Dates show the day (YYYY-MM-DD) but sort by the moment (`*_at`, seconds),
  // so papers from the same day keep the order in which they came.
  added:   { label: "added",    width: 10, cls: "da", sort: (i) => i.added_at ?? -Infinity,
             text: (i) => i.added ?? "" },
  modified: { label: "modified", width: 10, cls: "da", sort: (i) => i.modified_at ?? -Infinity,
             text: (i) => i.modified ?? "" },
  // When the paper entered refs (its record was made), as opposed to the
  // PDF's own dates above: useful for "what did I collect last week".
  in_refs: { label: "in refs", name: "added to refs", width: 10, cls: "da", sort: (i) => i.in_refs_at ?? -Infinity,
             text: (i) => i.in_refs ?? "" },
};
COLUMNS.st.name = "status"; COLUMNS.abbr.name = "journal abbreviation"; COLUMNS.rd.name = "queue / reading";
const DEFAULT_LAYOUT = { order: ["rd", "st", "authors", "year", "title", "abbr", "venue", "added", "in_refs", "modified"],
  hidden: [], widths: {}, sort: null, side: null, detail: null };

// Column order, widths, sort and pane sizes are remembered in this browser.
function loadLayout() {
  try {
    const saved = JSON.parse(localStorage.getItem("refs-layout"));
    if (saved?.order) {
      // Keep the saved order, drop columns that no longer exist, and put any
      // column added to the app since the layout was saved where it goes by
      // default: right after the column it follows there (or first).
      const order = saved.order.filter((id) => id in COLUMNS);
      DEFAULT_LAYOUT.order.forEach((id, at) => {
        if (order.includes(id)) return;
        const before = DEFAULT_LAYOUT.order.slice(0, at).reverse().find((x) => order.includes(x));
        order.splice(before ? order.indexOf(before) + 1 : 0, 0, id);
      });
      return { ...DEFAULT_LAYOUT, ...saved, order, hidden: saved.hidden ?? [] };
    }
  } catch {}
  return structuredClone(DEFAULT_LAYOUT);
}
function saveLayout() {
  try { localStorage.setItem("refs-layout", JSON.stringify(layout)); } catch {}
}
function applyLayout() {
  const root = document.documentElement.style;
  root.setProperty("--cols", visibleColumns().map((id) => COLUMNS[id].width === null
    ? "minmax(14ch, 1fr)"
    // A fixed column ignores any width saved for it earlier.
    : `${COLUMNS[id].fixed ? COLUMNS[id].width : layout.widths[id] ?? COLUMNS[id].width}ch`).join(" "));
  if (layout.side) root.setProperty("--side-w", `${layout.side}px`);
  if (layout.detail) root.setProperty("--detail-w", `${layout.detail}px`);
}
const visibleColumns = () => layout.order.filter((id) => !layout.hidden.includes(id));
let layout = loadLayout();

// --- talking to the server ---------------------------------------------------

async function get(url) {
  return (await fetch(url)).json();
}
async function post(url, body) {
  // The X-Refs header is what the server checks before changing anything.
  const response = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Refs": "1" },
    body: JSON.stringify(body ?? {}),
  });
  return response.json();
}
function say(text, ms = 4000) {
  $("message").textContent = text;
  clearTimeout(say.timer);
  if (ms) say.timer = setTimeout(() => ($("message").textContent = ""), ms);
}

// --- sidebar -------------------------------------------------------------------

async function loadSidebar() {
  state.sidebar = await get("/api/sidebar");
  drawSidebar();
}

// --- sidebar preferences ---------------------------------------------------------------
// How each section looks, in this browser (like the column layout): its sort,
// folded or not, hidden or not, and whether empty collections show. Pins and
// hidden collections are different: they belong to the collections and are
// saved with them (collections.json), so they follow you to your other Macs.
const SORTS = { custom: "my order", term: "by term", az: "A–Z", newest: "newest first",
  recent: "recently added to", size: "most papers" };
const SORT_CHOICES = { pinned: ["custom", "az", "newest", "recent", "size"],
  project: ["custom", "az", "newest", "recent", "size"], course: ["term", "az", "newest", "recent", "size"] };
const SIDE_DEFAULT = { sort: { pinned: "custom", project: "custom", course: "term" },
  folded: {}, hidden: [], empty: {}, quiet: false };
// Sections that cannot be hidden: the way to everything else, and to settings.
const KEEP_SECTIONS = new Set(["library", "refs"]);

function loadSide() {
  try {
    const saved = JSON.parse(localStorage.getItem("refs-sidebar"));
    if (saved) return { ...structuredClone(SIDE_DEFAULT), ...saved, sort: { ...SIDE_DEFAULT.sort, ...saved.sort } };
  } catch {}
  return structuredClone(SIDE_DEFAULT);
}
let side = loadSide();
// Save and redraw (the sidebar, and the sidebar page if it is open).
function saveSide() {
  try { localStorage.setItem("refs-sidebar", JSON.stringify(side)); } catch {}
  drawSidebar();
  if (state.view === "sidebar" && !state.collection && !state.context && !state.tag) drawSidebarPage();
}

// The comparison for a sort mode. `field` holds "my order": `order` for
// projects, `pinned` for pins. Unknown dates and places go last.
function sorter(mode, field = "order") {
  const az = (a, b) => a.name.localeCompare(b.name);
  const desc = (key) => (a, b) => (b[key] ?? -Infinity) - (a[key] ?? -Infinity) || az(a, b);
  return {
    custom: (a, b) => (a[field] ?? Infinity) - (b[field] ?? Infinity) || az(a, b),
    term: (a, b) => termRank(b.term) - termRank(a.term) || az(a, b),
    newest: desc("created_at"), recent: desc("used_at"),
    size: (a, b) => b.works - a.works || az(a, b),
  }[mode] ?? az;
}

// Draw the sidebar from the last answer (state.sidebar). Hiding, pinning or
// moving changes that answer in place and redraws at once; the server is
// told meanwhile.
function drawSidebar() {
  const data = state.sidebar;
  if (!data) return;
  state.dump = data.dump || state.dump;
  // Each collection's details, for the reading-list headings (groupOf) and the menus.
  state.collectionInfo = Object.fromEntries((data.collections ?? []).map((c) => [c.name, c]));
  state.proxy = data.proxy ?? "";     // the library proxy (settings), "" = none
  const nav = (label, count, attrs, sub = false) =>
    `<div class="nav${sub ? " sub" : ""}" ${attrs}><span>${esc(label)}</span><span class="n">${count}</span></div>`;
  const sections = [];                 // [key, title, body], in order
  const add = (key, title, body) => sections.push([key, title, body]);

  let html = nav("all papers", data.presets.all, `data-view="all"`);
  html += nav("queue", data.presets.queue, `data-view="queue"`);
  html += nav("unread", data.presets.unread, `data-view="unread"`);
  html += nav("reading", data.presets.reading, `data-view="reading"`);
  html += nav("read", data.presets.read, `data-view="read"`);
  html += nav("no pdf yet", data.presets.wanted, `data-view="wanted"`);
  // The library folder itself (the quick-add folder, refs-library): its PDFs.
  const libraryFolder = data.contexts.find((c) => c.key === "dump");
  if (libraryFolder) html += nav(state.dump, libraryFolder.count, `data-context="dump" title="the library folder"`);
  add("library", "Library", html);

  add("pinned", "Pinned", pinnedNav(data.collections ?? []));

  // Things that want a decision from you. "Only what needs you" (the
  // section's menu) leaves out the ones at 0, except the one you are on.
  const review = (view, label, count) => (count == null || (side.quiet && !count && state.view !== view)) ? ""
    : nav(label, count, `data-view="${view}"`);
  html = review("review", "needs review", data.presets.review);
  html += review("renames", "renames", data.presets.renames);
  html += review("copies", "copies", data.presets.copies);
  // PDFs in ~/Downloads that look like papers (null: no Downloads folder set).
  html += review("downloads", "downloads", data.presets.downloads);
  // Papers in course (inbox) folders and shared folders that the library lacks.
  html += review("inbox", "inbox", data.presets.inbox);
  // PDFs that are on disk but were never read (an interrupted add, or a file
  // that arrived through Finder). Shown only when there are some.
  if (data.presets.unprocessed) html += nav("not identified", data.presets.unprocessed, `data-view="unprocessed"`);
  add("review", "Review", html || `<div class="nav more"><span>nothing to decide</span><span class="n"></span></div>`);

  // Collections (collection.py): projects, then courses.
  add("project", "Projects", collectionSection("project", data.collections ?? []));
  add("course", "Courses", collectionSection("course", data.collections ?? []));

  // Folders that are not collections (the quick-add folder; anything the
  // layout does not read as a course or project) arrive as keys like
  // "referee/econometrica", each with its area and term. The layout
  // (contexts.py) says which areas get a heading of their own, in order;
  // within an area that has terms, group by term, newest first.
  const areas = {};
  for (const { key, count, area, term, collection } of data.contexts) {
    if (collection) continue;                      // listed above, as a collection
    const name = area ?? key.split("/")[0];
    const rest = key.split("/").slice(1);          // the key below the area
    (areas[name] ??= []).push({ key, rest, count, term });
  }
  const listed = ["dump", ...data.areas];
  for (const area of listed) {
    if (!areas[area] || area === "dump") continue;
    html = "";
    let term = null;
    const termed = areas[area].some((c) => c.term);
    // Contexts without a term first (by name), then terms newest first; the
    // sort is stable, so a term's courses keep their alphabetical order.
    const entries = termed
      ? areas[area].sort((a, b) => termRank(b.term) - termRank(a.term))
      : areas[area];
    for (const { key, rest, count, term: t } of entries) {
      const grouped = Boolean(t) && rest.length > 1;
      if (grouped && t !== term) {
        term = t;
        html += `<div class="group">${esc(term)}</div>`;
      }
      const label = rest[rest.length - 1] ?? area;
      html += nav(label, count, `data-context="${esc(key)}"`, grouped);
    }
    add(`area:${area}`, area, html);
  }
  const shown = new Set(listed);
  html = "";
  for (const area of Object.keys(areas).filter((a) => !shown.has(a))) {
    for (const { key, count } of areas[area]) html += nav(key, count, `data-context="${esc(key)}"`);
  }
  add("other", "Other", html);
  add("fields", "Fields", data.fields.map(([tag, count]) => nav(tag, count, `data-tag="${esc(tag)}"`)).join(""));
  html = `<div class="nav" data-view="migrate"><span>migration</span><span class="n"></span></div>`;
  html += `<div class="nav" data-view="sidebar" title="sort, pin, hide, fold"><span>sidebar</span><span class="n"></span></div>`;
  html += `<div class="nav" data-view="settings"><span>settings</span><span class="n">,</span></div>`;
  add("refs", "refs", html);

  // Each section: a heading (click folds it, right-click has its options),
  // then its entries. A section with nothing in it is left out.
  state.sideSections = sections.filter(([, , body]) => body).map(([key, title]) => ({ key, title }));
  $("side").innerHTML = sections.map(([key, title, body]) => {
    if (!body || side.hidden.includes(key)) return "";
    const folded = Boolean(side.folded[key]);
    const sort = SORT_CHOICES[key] && side.sort[key] !== SORT_CHOICES[key][0] ? ` · ${SORTS[side.sort[key]]}` : "";
    return `<div class="label fold${folded ? " folded" : ""}" data-section="${esc(key)}"
      title="click to fold or unfold; right-click for its options"><span><i class="tw">${folded ? "▸" : "▾"}</i>${
      esc(title)}<span class="sortname">${esc(sort)}</span></span></div>` + (folded ? "" : body);
  }).join("");
  markSidebar();
}

// Pinned: shortcuts to any course, project or part, at the top. Each is
// still listed in its own section too.
function pinnedNav(list) {
  return list.filter((c) => c.pinned).sort(sorter(side.sort.pinned, "pinned")).map((c) =>
    `<div class="nav${c.hidden ? " hid" : ""}" data-collection="${esc(c.name)}" data-drag="pinned" draggable="true"
      title="#${esc(c.name)} (pinned): drag to move, right-click for more"><span>${esc(c.name + (c.shared ? " ⇄" : ""))}</span>` +
    `<span class="n">${c.works}</span></div>`).join("");
}

// A collection section (collection.py). Projects: your order by default (drag
// one, or "move up" in its menu), else the section's sort; courses: by term,
// newest first, else the sort. Each is followed by its parts in their
// reading-list order. Hidden ones (and, if asked, empty ones) are left out,
// behind a "N hidden" line at the end that shows them, dimmed.
const topName = (name) => name.split("/")[0];
state.showHidden = { project: false, course: false };   // the "N hidden" lines, unfolded

// The top-level collections of a section, in the order shown (hidden ones too).
function sectionTops(kind, list = state.sidebar?.collections ?? []) {
  return list.filter((c) => !c.name.includes("/") && (kind === "course") === (c.kind === "course"))
    .sort(sorter(side.sort[kind], "order"));
}
// The names in a draggable group (projects, or pins), in the order shown.
function groupOrder(group) {
  return group === "pinned"
    ? (state.sidebar?.collections ?? []).filter((c) => c.pinned).sort(sorter(side.sort.pinned, "pinned")).map((c) => c.name)
    : sectionTops("project").map((c) => c.name);
}
// Left out of a section unless its "N hidden" line is unfolded.
const tucked = (c, kind) => c.hidden || (side.empty[kind] && !c.works);

function collectionSection(kind, list) {
  const parts = {};                                   // parent name -> its parts
  for (const c of list) if (c.name.includes("/")) (parts[c.name.slice(0, c.name.lastIndexOf("/"))] ??= []).push(c);
  const open = state.showHidden[kind];
  const withParts = (c) => [c, ...(parts[c.name] ?? []).sort(byPlace).filter((p) => open || !tucked(p, kind)).flatMap(withParts)];
  const tops = sectionTops(kind, list);
  const entry = (c, top) => {
    const depth = c.name.split("/").length - 1;
    const label = c.name.split("/").pop() + (c.shared ? " ⇄" : "");
    const away = tucked(top, kind) || tucked(c, kind);
    const about = [c.kind, c.term, c.shared ? "shared with coauthors" : "", c.implicit ? "from its folder" : "",
                   c.folder ? `folder ${c.folder}` : "", c.pinned ? "pinned" : "", away ? "hidden" : ""].filter(Boolean).join(" · ");
    // A top-level project can be dragged to another place among the projects.
    const drag = c === top && kind === "project" ? ` draggable="true" data-drag="project"` : "";
    const hint = drag ? "; drag to move, right-click for more" : "; right-click for more";
    return `<div class="nav${away ? " hid" : ""}" data-collection="${esc(c.name)}"${drag} title="#${esc(c.name)}: ${esc(about)}${hint}"
      style="padding-left:${4 + 2 * depth}ch"><span>${esc(label)}</span><span class="n">${c.works}</span></div>`;
  };
  let html = "", term;
  const away = list.filter((c) => (kind === "course") === (state.collectionInfo[topName(c.name)]?.kind === "course")
    && (tucked(c, kind) || tucked(state.collectionInfo[topName(c.name)] ?? c, kind))).length;
  for (const top of tops) {
    if (tucked(top, kind) && !open) continue;
    // Courses by term: a heading per term (right-click it to hide the term's courses).
    if (kind === "course" && side.sort.course === "term" && (top.term ?? null) !== term) {
      term = top.term ?? null;
      html += `<div class="group" data-term="${esc(term ?? "")}">${esc(term ?? "no term")}</div>`;
    }
    for (const c of withParts(top)) html += entry(c, top);
  }
  if (away) html += `<div class="nav more" data-show-hidden="${kind}" title="hidden collections${
    side.empty[kind] ? " and empty ones" : ""}: click to show them">` +
    `<span>${open ? "▾" : "▸"} ${away} hidden</span><span class="n"></span></div>`;
  // The section ends with its own "+ new", which makes one of that kind.
  return html + `<div class="nav" data-newcoll="${kind}" style="padding-left:4ch">` +
    `<span>+ new ${kind}</span><span class="n"></span></div>`;
}
const byPlace = (a, b) => (a.order ?? Infinity) - (b.order ?? Infinity) || a.name.localeCompare(b.name);

function markSidebar() {
  for (const el of $("side").querySelectorAll(".nav")) {
    const on = el.dataset.collection ? el.dataset.collection === state.collection
      : el.dataset.context ? el.dataset.context === state.context
      : el.dataset.tag ? el.dataset.tag === state.tag
      : !state.context && !state.tag && !state.collection && el.dataset.view === state.view;
    el.classList.toggle("on", on);
  }
}

$("side").addEventListener("click", (event) => {
  const heading = event.target.closest(".label[data-section]");
  if (heading) { foldSection(heading.dataset.section); return; }
  const el = event.target.closest(".nav");
  if (!el) return;
  if (el.dataset.newcoll) { renderNewCollection(el.dataset.newcoll); return; }
  if (el.dataset.showHidden) {
    state.showHidden[el.dataset.showHidden] = !state.showHidden[el.dataset.showHidden];
    drawSidebar();
    return;
  }
  state.collection = el.dataset.collection ?? null;
  state.context = el.dataset.context ?? null;
  state.tag = el.dataset.tag ?? null;
  state.view = el.dataset.view ?? "all";
  markSidebar();
  search();
});

// --- sidebar: moving, pinning, hiding, folding -------------------------------------------
// Places, pins and hidden collections are saved in the registry
// (collections.json, in the library), so they sync to your other Macs; none
// of it touches a paper or a folder.

let sideDrag = null;                // { name, group } while a project or pin is dragged
const clearDrop = () => $("side").querySelectorAll(".drop-before, .drop-after, .dragged")
  .forEach((el) => el.classList.remove("drop-before", "drop-after", "dragged"));
$("side").addEventListener("dragstart", (event) => {
  const el = event.target.closest?.(".nav[data-drag]");
  if (!el) return;
  sideDrag = { name: el.dataset.collection, group: el.dataset.drag };
  event.dataTransfer.effectAllowed = "move";
  event.dataTransfer.setData("text/x-refs-collection", sideDrag.name);   // not "Files": the drop-a-PDF code ignores it
  el.classList.add("dragged");
});
// A drop target: another entry of the same group (a project among projects, a pin among pins).
const dropTarget = (event) => {
  const el = event.target.closest?.(".nav[data-drag]");
  return el && sideDrag && el.dataset.drag === sideDrag.group && el.dataset.collection !== sideDrag.name ? el : null;
};
$("side").addEventListener("dragover", (event) => {
  if (!sideDrag) return;
  $("side").querySelectorAll(".drop-before, .drop-after").forEach((x) => x.classList.remove("drop-before", "drop-after"));
  const el = dropTarget(event);
  if (!el) return;
  event.preventDefault();
  const box = el.getBoundingClientRect();
  el.classList.add(event.clientY < box.top + box.height / 2 ? "drop-before" : "drop-after");
});
$("side").addEventListener("drop", (event) => {
  const el = dropTarget(event);
  if (!el) return;
  event.preventDefault();
  placeIn(sideDrag.group, sideDrag.name, el.dataset.collection, el.classList.contains("drop-after"));
});
$("side").addEventListener("dragend", () => { sideDrag = null; clearDrop(); });

// Put a project (or pin) just before, or after, another one. The section
// switches to "my order", starting from the order it showed.
function placeIn(group, name, target, after = false) {
  if (name === target) return;
  const names = groupOrder(group).filter((n) => n !== name);
  names.splice(names.indexOf(target) + (after ? 1 : 0), 0, name);
  arrangeIn(group, names);
}

// Move one place up or down among those shown, or to the top.
function stepIn(group, name, how) {
  const shown = groupOrder(group).filter((n) => group === "pinned" || state.showHidden.project
    || !tucked(state.collectionInfo[n] ?? {}, "project"));
  const at = shown.indexOf(name);
  if (how === "top") placeIn(group, name, shown[0]);
  else if (how === "up" && at > 0) placeIn(group, name, shown[at - 1]);
  else if (how === "down" && at < shown.length - 1) placeIn(group, name, shown[at + 1], true);
}

// `names` in this order (1, 2, …): projects (`order`) or pins (`pinned`).
async function arrangeIn(group, names) {
  const field = group === "pinned" ? "pinned" : "order";
  names.forEach((n, at) => { if (state.collectionInfo[n]) state.collectionInfo[n][field] = at + 1; });
  side.sort[group] = "custom";
  saveSide();
  const answer = await post("/api/collections/arrange", { names, field });
  if (answer.detail) { say(answer.detail, 8000); loadSidebar(); return; }
  collectionList = null;
}

async function hideCollections(names, hidden = true) {
  if (!names.length) return;
  for (const n of names) if (state.collectionInfo[n]) state.collectionInfo[n].hidden = hidden;
  saveSide();
  const answer = await post("/api/collections/hide", { names, hidden });
  if (answer.detail) { say(answer.detail, 8000); loadSidebar(); return; }
  collectionList = null;
  const what = names.length === 1 ? `#${names[0]}` : `${names.length} collections`;
  say(hidden ? `hid ${what} (still found by #name)` : `${what} back in the sidebar`, 8000);
}

async function pinCollections(names, pinned = true) {
  if (!names.length) return;
  // New pins go after the others, as the server places them.
  let last = Math.max(0, ...Object.values(state.collectionInfo).map((c) => c.pinned ?? 0));
  for (const n of names) {
    const c = state.collectionInfo[n];
    if (c) c.pinned = pinned ? (c.pinned || ++last) : null;
  }
  saveSide();
  const answer = await post("/api/collections/pin", { names, pinned });
  if (answer.detail) { say(answer.detail, 8000); loadSidebar(); return; }
  const what = names.length === 1 ? `#${names[0]}` : `${names.length} collections`;
  say(pinned ? `pinned ${what}` : `unpinned ${what}`);
}

function foldSection(key, folded = !side.folded[key]) {
  if (folded) side.folded[key] = true;
  else delete side.folded[key];
  saveSide();
}

function hideSection(key, hidden = true) {
  if (KEEP_SECTIONS.has(key)) return;
  side.hidden = hidden ? [...new Set([...side.hidden, key])] : side.hidden.filter((k) => k !== key);
  saveSide();
  if (hidden) say(`hid ${sectionTitle(key)} (sidebar, under refs, shows it again)`, 8000);
}
const sectionTitle = (key) => state.sideSections?.find((s) => s.key === key)?.title ?? key;

// --- list ------------------------------------------------------------------------

function authorsShort(names) {
  if (!names?.length) return "";
  return names.length > 3 ? `${names[0]} et al.` : names.join(", ");
}

// Every load of the list gets a number; an answer that arrives after a newer
// load started is dropped, so a slow search never overwrites the view you
// switched to in the meantime.
let listToken = 0;

// `ask`: "off" while typing (local search only, instant); "auto" on Enter, which
// lets the server ask Haiku when the query reads like a description and the
// local results look unsure, or when it starts with "?".
async function search(keepId = null, ask = "off") {
  const token = ++listToken;
  if (REVIEW_VIEWS[state.view] && !state.context && !state.tag && !state.collection) {
    return REVIEW_VIEWS[state.view](token);
  }
  // The sidebar selection becomes ordinary filters in front of the typed query.
  const parts = [];
  if (state.collection) parts.push(`#${state.collection}`);
  if (state.context) parts.push(state.context === "dump" ? "area:dump" : `in:${state.context}`);
  if (state.tag) parts.push(`tag:${state.tag.replace(/\s+/g, "-")}`);
  if (state.query) parts.push(state.query);
  // The typed query goes first, so a leading "?" (always ask Haiku) stays leading.
  if (state.query) parts.unshift(parts.pop());
  const url = `/api/search?q=${encodeURIComponent(parts.join(" "))}&view=${state.view}` +
    `&scope=${state.scope}&ask=${ask}`;
  // Asking Haiku takes a few seconds: say so if the answer is slow to come.
  const waiting = ask !== "off" && setTimeout(() => say("asking Haiku …", 0), 400);
  const data = await get(url);
  clearTimeout(waiting);
  if (token !== listToken) return;
  state.raw = data.items;                 // the server's order, kept for "no sort"
  state.asked = Boolean(data.asked);      // Haiku chose this order: keep it unsorted
  if (data.error) say(data.error);
  else if (data.message) say(`✦ ${data.message}`, 9000);
  else if (ask !== "off") say("");
  sortItems();
  for (const item of state.items) if (state.marked.has(item.id)) state.marked.set(item.id, item);
  const kept = keepId ? state.items.findIndex((i) => i.id === keepId) : -1;
  state.selected = Math.max(0, kept);
  renderList();
  renderDetail();
}

// Where a paper sits in the reading list being shown: {order, label, heading}.
// In a collection: the part it is in (219a/public-economics, ordered and
// labelled by the collection's details), else the readings/<topic> folder its
// PDF sits in (before the migration), else "other", last. In a folder: the topic.
const pad = (n) => (n != null ? String(n).padStart(2, "0") + " " : "");
function groupOf(item) {
  if (state.collection) {
    const part = (item.collections ?? []).find((n) => n.startsWith(state.collection + "/"));
    if (part) {
      const info = state.collectionInfo?.[part] ?? {};
      const label = info.label ?? part.slice(state.collection.length + 1).replace(/-/g, " ");
      return { order: info.order ?? 500, label, heading: pad(info.order) + label };
    }
    const top = state.collection.split("/")[0];
    const loc = item.locations.find((l) => l.topic && (l.slug ?? "").toLowerCase() === top);
    if (loc) return { order: loc.topic_order ?? 500, label: loc.topic, heading: pad(loc.topic_order) + loc.topic };
    return { order: 999, label: "", heading: "other" };
  }
  const loc = item.locations.find((l) => l.context === state.context) ?? item.locations[0];
  const label = loc?.topic ?? loc?.section ?? "unfiled";
  return { order: loc?.topic_order ?? 999, label, heading: pad(loc?.topic_order) + label };
}

function sortItems() {
  state.items = [...(state.raw ?? [])];
  const chosen = state.asked ? null : layout.sort;
  if (chosen) {
    const key = COLUMNS[chosen.col].sort, direction = chosen.dir === "desc" ? -1 : 1;
    state.items.sort((a, b) => {
      const x = key(a), y = key(b);
      return (x < y ? -1 : x > y ? 1 : 0) * direction;
    });
  } else if (state.collection && !state.query) {
    // A collection: like a reading list, part by part (groupOf), each part's
    // papers in the server's order.
    const at = new Map(state.items.map((item, i) => [item, i]));
    state.items.sort((a, b) => {
      const x = groupOf(a), y = groupOf(b);
      return x.order - y.order || x.label.localeCompare(y.label) || at.get(a) - at.get(b);
    });
  } else if (!state.context && !state.query) {
    // No column chosen, no course, nothing typed: list like a bibliography.
    const key = (i) => `${(i.authors?.[0] ?? last).toLowerCase()} ${i.year ?? 9999}`;
    state.items.sort((a, b) => key(a).localeCompare(key(b)));
  }
}

function renderHead() {
  return `<div class="head">` + visibleColumns().map((id) => {
    const column = COLUMNS[id];
    const arrow = layout.sort?.col === id ? (layout.sort.dir === "desc" ? "▼" : "▲") : "";
    // No resize handle on the column that takes the rest, nor on one-glyph columns.
    const grip = column.width === null || column.fixed ? "" : `<b class="grip" data-col="${id}"></b>`;
    // The label sits on the same edge as the column's contents; the sort arrow
    // goes on the inner side, so the label itself stays flush with the data.
    const label = `<span class="hl">${esc(column.label)}</span>`;
    const mark = arrow ? `<i>${arrow}</i>` : "";
    // A one-glyph column has no room for both: the arrow replaces the glyph.
    const content = column.fixed ? (arrow ? mark : label)
      : column.align === "right" ? mark + label : label + mark;
    const what = column.fixed ? `sort by ${column.name}; ` : "";
    return `<span class="hc ${column.align ?? "left"}${column.fixed ? " glyph" : ""}" draggable="true" data-col="${id}"
      title="${what}click to sort, drag to move">${content}${grip}</span>`;
  }).join("") + `</div>`;
}

function renderList() {
  const items = state.items;
  $("count").textContent = `${items.length} ${state.scope === "all" ? "items" : "works"}`;
  if (!items.length) {
    // An empty collection (a typo in the box, a finished course) can go.
    const drop = state.collection && !state.query
      ? ` <button class="act" data-coll-delete="${esc(state.collection)}">delete #${esc(state.collection)}</button>` : "";
    $("list").innerHTML = renderHead() + `<div class="empty">nothing here.${drop}</div>`;
    return;
  }
  // Inside a course (a collection or a folder), with no search words and no
  // chosen sort, show the reading-list structure: a heading whenever the
  // part or topic changes.
  const grouped = (state.context || state.collection) && !state.query && !layout.sort;
  let html = renderHead(), lastTopic;
  items.forEach((item, i) => {
    const group = grouped ? groupOf(item) : null;
    if (group && group.heading !== lastTopic) {
      lastTopic = group.heading;
      html += `<div class="topic">${esc(group.heading)}</div>`;
    }
    const cls = ["row", i === state.selected ? "sel" : "", state.marked.has(item.id) ? "mk" : ""].join(" ");
    html += `<div class="${cls}" data-i="${i}">` + visibleColumns().map((id) => {
      const column = COLUMNS[id];
      return column.cell ? column.cell(item)
        : `<span class="${column.cls} ${column.align ?? "left"}">${esc(column.text(item))}</span>`;
    }).join("") + `</div>`;
  });
  $("list").innerHTML = html;
}

// --- header: click to sort, drag to reorder, drag the edge to resize ---------------

let resizing = null;      // { id, startX, startWidth } while a column edge is dragged
let draggedColumn = null;
const charWidth = (() => {              // width of one character, to convert pixels to ch
  const probe = document.createElement("span");
  probe.textContent = "0000000000";
  probe.style.cssText = "position:absolute;visibility:hidden;font:inherit";
  document.body.appendChild(probe);
  const width = probe.getBoundingClientRect().width / 10;
  probe.remove();
  return width || 8;
})();

$("list").addEventListener("click", (event) => {
  const head = event.target.closest(".hc");
  if (!head || event.target.closest(".grip")) return;
  // Cycle: ascending, descending, back to the default order.
  const id = head.dataset.col, current = layout.sort;
  setSort(current?.col !== id ? { col: id, dir: "asc" }
    : current.dir === "asc" ? { col: id, dir: "desc" } : null);
});

// Sort the list by a column ({col, dir}), or null for the default order.
function setSort(sort) {
  layout.sort = sort;
  saveLayout();
  const keep = state.items[state.selected]?.id;
  sortItems();
  state.selected = Math.max(0, state.items.findIndex((i) => i.id === keep));
  renderList();
}

$("list").addEventListener("pointerdown", (event) => {
  const grip = event.target.closest(".grip");
  if (!grip) return;
  event.preventDefault();
  const id = grip.dataset.col;
  resizing = { id, startX: event.clientX, startWidth: layout.widths[id] ?? COLUMNS[id].width };
  document.body.classList.add("dragging");
});
$("list").addEventListener("dragstart", (event) => {
  const head = event.target.closest(".hc");
  if (!head || resizing) { event.preventDefault(); return; }
  draggedColumn = head.dataset.col;
  event.dataTransfer.effectAllowed = "move";
});
$("list").addEventListener("dragover", (event) => {
  if (draggedColumn && event.target.closest(".hc")) event.preventDefault();
});
$("list").addEventListener("drop", (event) => {
  const target = event.target.closest(".hc");
  if (!draggedColumn || !target || target.dataset.col === draggedColumn) return;
  event.preventDefault();
  const order = layout.order.filter((id) => id !== draggedColumn);
  order.splice(order.indexOf(target.dataset.col), 0, draggedColumn);
  layout.order = order;
  draggedColumn = null;
  saveLayout(); applyLayout(); renderList();
});

// --- columns menu: show or hide each column -----------------------------------------

function renderColumnMenu() {
  $("colmenu").innerHTML = layout.order.map((id) => {
    const shown = !layout.hidden.includes(id);
    const name = COLUMNS[id].name ?? COLUMNS[id].label;
    return `<div data-col="${id}" class="${shown ? "" : "off"}">[${shown ? "x" : " "}] ${esc(name)}</div>`;
  }).join("");
}
$("colbutton").addEventListener("click", (event) => {
  event.stopPropagation();
  renderColumnMenu();
  $("colmenu").hidden = !$("colmenu").hidden;
});
$("colmenu").addEventListener("click", (event) => {
  event.stopPropagation();                       // keep the menu open while toggling
  const id = event.target.closest("[data-col]")?.dataset.col;
  if (!id) return;
  const shown = !layout.hidden.includes(id);
  // Never hide the last visible column.
  if (shown && visibleColumns().length === 1) return;
  layout.hidden = shown ? [...layout.hidden, id] : layout.hidden.filter((x) => x !== id);
  if (layout.sort && layout.hidden.includes(layout.sort.col)) { layout.sort = null; sortItems(); }
  saveLayout(); applyLayout(); renderColumnMenu(); renderList();
});
document.addEventListener("click", () => { $("colmenu").hidden = true; $("watchpanel").hidden = true; });

// --- pane dividers -----------------------------------------------------------------

let splitting = null;     // "side" or "detail" while a divider is dragged
for (const divider of document.querySelectorAll(".split")) {
  divider.addEventListener("pointerdown", (event) => {
    event.preventDefault();
    resizing = null;
    splitting = divider.dataset.pane;
    document.body.classList.add("dragging");
  });
}
window.addEventListener("pointermove", (event) => {
  if (resizing) {
    const delta = (event.clientX - resizing.startX) / charWidth;
    layout.widths[resizing.id] = Math.max(1, Math.round(resizing.startWidth + delta));
    applyLayout();
  } else if (splitting) {
    const box = document.querySelector("main").getBoundingClientRect();
    const width = splitting === "side" ? event.clientX - box.left : box.right - event.clientX;
    layout[splitting] = Math.round(Math.max(120, Math.min(box.width * 0.6, width)));
    applyLayout();
  }
});
window.addEventListener("pointerup", () => {
  if (resizing || splitting) saveLayout();
  // Clear on the next tick, so the click that ends a resize does not also sort.
  setTimeout(() => { resizing = null; }, 0);
  splitting = null;
  document.body.classList.remove("dragging");
});

function select(i, scroll = true) {
  if (!state.items.length) return;
  state.selected = Math.max(0, Math.min(state.items.length - 1, i));
  for (const row of $("list").querySelectorAll(".row")) {
    row.classList.toggle("sel", Number(row.dataset.i) === state.selected);
  }
  if (scroll) $("list").querySelector(".row.sel")?.scrollIntoView({ block: "nearest" });
  return renderDetail();
}

// Plain click: select one row (and drop any batch selection, as in Finder).
// ⌘-click: add or remove a row; ⇧-click: add the range from the selected row.
$("list").addEventListener("click", (event) => {
  const row = event.target.closest(".row");
  if (!row || !state.items.length) return;
  const i = Number(row.dataset.i);
  if (event.metaKey || event.ctrlKey) {
    if (!state.marked.size) markRow(state.selected, true);   // the row you were on counts too
    markRow(i);
  } else if (event.shiftKey) {
    const [from, to] = [state.selected, i].sort((a, b) => a - b);
    for (let n = from; n <= to; n++) markRow(n, true);
  } else if (state.marked.size) {
    state.marked.clear();
  }
  select(i, false);
  if (event.metaKey || event.ctrlKey || event.shiftKey || !state.marked.size) paintMarks();
});
// ⇧-click would otherwise select the text between the two rows.
$("list").addEventListener("mousedown", (event) => {
  if (event.shiftKey && event.target.closest(".row")) event.preventDefault();
});
$("list").addEventListener("dblclick", (event) => { if (event.target.closest(".row")) act("open"); });

// --- batch selection ------------------------------------------------------------------
// Marked rows (state.marked) get one set of actions in the detail pane: file
// to… / copy to… a folder (a new one too), queue, mark read.

function markRow(i, on) {
  const item = state.items[i];
  if (!item) return;
  if (on ?? !state.marked.has(item.id)) state.marked.set(item.id, item);
  else state.marked.delete(item.id);
}
function paintMarks() {
  for (const row of $("list").querySelectorAll(".row")) {
    row.classList.toggle("mk", state.marked.has(state.items[Number(row.dataset.i)]?.id));
  }
  renderDetail();
}
function clearMarks() { state.marked.clear(); paintMarks(); }

// The PDF a batch action moves for each paper: the copy in the quick-add folder
// if there is one (the usual "file it from the dump"), else the one in the
// course or project you are looking at, else its first.
function primaryPath(item) {
  const locs = item.locations ?? [];
  const inDump = (l) => l.path === state.dump || l.path.startsWith(state.dump + "/");
  return (locs.find(inDump) ?? locs.find((l) => l.context === state.context) ?? locs[0])?.path ?? null;
}

function renderBatch() {
  const items = [...state.marked.values()];
  const withPdf = items.filter(primaryPath);
  let html = `<h1>${items.length} selected</h1>
    <p class="where">⌘-click or <kbd>x</kbd> adds or removes a row, ⇧-click a range, <kbd>esc</kbd> clears.
    The selection stays while you search, so you can gather papers from several searches.</p>
    <div class="acts" style="margin-top:6px">
      <button class="act" data-batch="move" ${withPdf.length ? "" : "disabled"}>file to…</button>
      <button class="act" data-batch="copy" ${withPdf.length ? "" : "disabled"}>copy to…</button>
      <button class="act" data-batch="collect">add to collection…</button>
      ${state.collection ? `<button class="act" data-batch="uncollect">take out of #${esc(state.collection)}</button>` : ""}
      <button class="act" data-batch="queue">queue all</button>
      <button class="act" data-batch="read">mark read</button>
      <button class="act" data-batch="clear">clear</button>
    </div><div class="picker" id="batchpicker"></div>`;
  if (withPdf.length < items.length) {
    html += `<p class="where">${items.length - withPdf.length} of them have no PDF yet and are left out of filing.</p>`;
  }
  html += `<div class="label">Selected</div>`;
  for (const item of items) {
    const path = primaryPath(item);
    const who = item.authors?.length ? `${authorsShort(item.authors)} (${item.year ?? "n.d."}) ` : "";
    html += `<div class="loc"><div>${esc(who + (item.title ?? path?.split("/").pop() ?? ""))}</div>
      <div class="path">${esc(path ? path.slice(0, path.lastIndexOf("/")) : "no pdf")}</div></div>`;
  }
  $("detail").innerHTML = html;
  $("detail").dataset.bibtex = "";
  currentWork = null;
}

async function batchAct(name) {
  const items = [...state.marked.values()];
  if (name === "clear") { clearMarks(); return; }
  if (name === "move" || name === "copy") {
    const paths = items.map(primaryPath).filter(Boolean);
    if (paths.length) openPicker(paths, name, $("batchpicker"));
    return;
  }
  const ids = items.map((i) => i.work_id).filter(Boolean);
  if (name === "collect") { openCollectionPicker(ids, $("batchpicker")); return; }
  if (name === "uncollect") { collect(ids, state.collection, false); return; }
  // Queue or mark read, one paper at a time (each is a small write).
  const works = items.filter((i) => i.work_id);
  for (const item of works) {
    const done = name === "queue"
      ? await post(`/api/work/${item.work_id}/queue`, { on: true })
      : await post(`/api/work/${item.work_id}/read`, { status: "read" });
    if (!done.detail) Object.assign(item, done);
  }
  say(`${name === "queue" ? "queued" : "marked read"}: ${works.length} papers`);
  loadSidebar(); search(state.items[state.selected]?.id ?? null);
}

// --- detail ------------------------------------------------------------------------

let detailToken = 0;
let detailDone = Promise.resolve();   // the detail pane's latest draw (menu actions wait for it)
function renderDetail() { return (detailDone = drawDetail()); }
async function drawDetail() {
  if (state.marked.size) { ++detailToken; renderBatch(); return; }
  const item = state.items[state.selected];
  if (!item) { $("detail").innerHTML = ""; return; }
  const token = ++detailToken;        // ignore a slow answer for a row we already left

  // Full metadata (first names, volume, pages, BibTeX) exists only for works.
  let work = null, bibtex = "", noteData = null, copies = [];
  if (item.work_id) {
    const data = await get(`/api/work/${item.work_id}`);
    if (token !== detailToken) return;
    work = data.work; bibtex = data.bibtex; noteData = data.note; copies = data.copies ?? [];
  }
  const copyAt = Object.fromEntries(copies.map((c) => [c.path, c]));
  const meta = work?.meta ?? {};
  const authors = meta.authors?.length
    ? meta.authors.map((a) => [a.given, a.family].filter(Boolean).join(" ")).join(", ")
    : (item.authors ?? []).join(", ");
  const issue = [meta.volume, meta.issue && `(${meta.issue})`].filter(Boolean).join("");
  const where = [item.venue, item.year, issue, meta.pages].filter(Boolean).join(" · ");
  const status = item.status ?? "classified";

  let html = item.why ? `<p class="why">✦ ${esc(item.why)}</p>` : "";
  html += `<h1>${esc(item.title ?? "untitled")}</h1>`;
  if (authors) html += `<p class="who">${esc(authors)}</p>`;
  if (where) html += `<p class="where">${esc(where)}</p>`;

  html += `<div class="label">Record</div><dl class="meta">`;
  html += `<dt>status</dt><dd><span class="st ${status}">${GLYPH[status] ?? "·"}</span> ${esc(status)}${
    meta.source ? ` <span class="badge">· ${esc(meta.source === "pdf" ? "read from the pdf" : "from " + meta.source)}</span>` : ""}</dd>`;
  if (item.citekey) html += `<dt>citekey</dt><dd>${esc(item.citekey)}</dd>`;
  // Keys the project .bib files use for this paper (learned by refs).
  for (const [key, file] of Object.entries(work?.bib_keys ?? {})) {
    html += `<dt>also</dt><dd>${esc(key)} <span class="badge">· ${esc(file.replace(/^(~|Dropbox)\//, ""))}</span></dd>`;
  }
  if (meta.doi) html += `<dt>doi</dt><dd><a href="https://doi.org/${esc(meta.doi)}" target="_blank" rel="noreferrer">${esc(meta.doi)}</a></dd>`;
  html += `<dt>kind</dt><dd>${esc(item.kind)}</dd>`;
  if (item.read_status) {
    const auto = work?.read_auto;
    const how = item.read_source === "detected" && auto
      ? ` <span class="badge">· from your markup on ${auto.pages}/${auto.of} pages</span>` : "";
    html += `<dt>read</dt><dd>${esc(item.read_status)}${how}</dd>`;
  }
  if (item.queued) html += `<dt>queue</dt><dd><span class="rd q">▸</span> since ${esc(item.queued.slice(0, 10))}</dd>`;
  // The detail pane gives the time too (the list shows only the day).
  const when = (day, at) => esc(day) + (at != null ? ` <span class="badge">${clock(at)}</span>` : "");
  if (item.added) html += `<dt>added</dt><dd>${when(item.added, item.added_at)}</dd>`;
  if (item.in_refs) html += `<dt>in refs</dt><dd>${when(item.in_refs, item.in_refs_at)}</dd>`;
  if (item.modified) html += `<dt>modified</dt><dd>${when(item.modified, item.modified_at)}</dd>`;
  html += `</dl>`;
  if (work) {
    // Corrections: edit any field by hand, pull the record for a DOI you know,
    // or accept the record as it stands. All three clear "needs review".
    html += `<div class="acts" style="margin-top:6px">
      <button class="act" data-act="edit">edit</button>
      ${["needs-review", "extracted", "imported"].includes(status)
        ? `<button class="act" data-act="confirm">accept as is</button>` : ""}
      ${work.user?.overrides && Object.keys(work.user.overrides).length
        ? `<button class="act" data-act="revert">undo my edits</button>` : ""}
    </div><div id="editor"></div>`;
  }
  if (item.work_id) html += collectionsHtml(item);
  if (work && noteData) html += noteHtml(work, noteData);
  if (work?.user?.notes) html += `<div class="label">From Zotero</div><p class="notes">${esc(work.user.notes)}</p>`;

  // A work with several PDFs keeps the description of its best one.
  const about = work?.description ?? item.description;
  if (about) html += `<div class="label">About</div><p>${esc(about)}</p>`;
  if (item.tags?.length) {
    html += `<div class="label">Tags</div><div class="tags">` +
      item.tags.map((t) => `<span class="tag" data-tag="${esc(t)}">${esc(t)}</span>`).join("") + `</div>`;
  }

  if (!item.status && item.locations.length) {
    html += `<p class="where" style="margin-top:8px">This PDF is on disk but has not been read yet.</p>
      <div class="acts"><button class="act" data-identify="${esc(item.locations[0].path)}">identify it now</button></div>`;
  }
  html += `<div class="label">Files</div>`;
  if (!item.locations.length) {
    html += `<p class="where">no pdf yet. Save it into ${esc(state.dump)} and it attaches itself.</p>`;
    // Paywalled? Open its page through the library proxy (settings), signed in as you.
    const page = work?.user?.source_url || (meta.doi ? `https://doi.org/${meta.doi}` : "");
    if (state.proxy && page) {
      const via = state.proxy + (/qurl=$/i.test(state.proxy) ? encodeURIComponent(page) : page);
      html += `<div class="acts"><a class="act" href="${esc(via)}" target="_blank" rel="noreferrer">through the library</a></div>`;
    }
  }
  for (const loc of item.locations) {
    const copy = copyAt[loc.path];
    const place = copy?.where === "library" ? "library copy"
      : copy?.shared ? `#${copy.shared} copy · shared with coauthors`
      : [loc.context, loc.topic ?? loc.section].filter(Boolean).join(" · ");
    html += `<div class="loc" data-path="${esc(loc.path)}"><div>${esc(place)}</div>
      <div class="path">${esc(loc.path.split("/").pop())}</div>${marksLine(copy, item.locations.length > 1)}
      <div class="acts">
        <a class="act" href="/file?path=${encodeURIComponent(loc.path)}" target="_blank">open</a>
        <button class="act" data-open="${esc(loc.path)}">preview.app</button>
        <button class="act" data-reveal="${esc(loc.path)}">finder</button>
        <button class="act" data-fileto="${esc(loc.path)}">file to…</button>
        <button class="act" data-copyto="${esc(loc.path)}">copy to…</button>
      </div><div class="picker" data-for="${esc(loc.path)}"></div></div>`;
  }

  if (work?.user?.former_names?.length) {
    html += `<div class="label">Former file names</div>` +
      work.user.former_names.map((n) => `<div class="path">${esc(n)}</div>`).join("");
  }
  if (bibtex) {
    html += `<div class="label">BibTeX</div><pre class="bib">${esc(bibtex)}</pre>`;
    html += `<div class="acts" style="margin-top:6px">
      <button class="act" data-act="citekey">copy citekey</button>
      <button class="act" data-act="bibtex">copy bibtex</button>
      <button class="act" data-act="read">${item.read_status === "read" ? "mark unread" : "mark read"}</button>
      <button class="act" data-act="queue">${item.queued ? "off the queue" : "queue"}</button>
    </div>`;
  }
  $("detail").innerHTML = html;
  $("detail").dataset.bibtex = bibtex;
  currentWork = work;
  if (work && noteData) {
    noteSaved[work.id] = { version: noteData.version, body: noteData.body };
    noteConflict.delete(work.id);    // freshly read: saves can go ahead again
  }
}

// One copy's marks: yours, then others' by name, and the newest mark's date.
// ⚑ when it has marks the library copy lacks or newer ones (annotations.py);
// a flagged copy can bring them into the library copy.
const shortDay = (iso) => new Date(iso).toLocaleDateString([], { day: "numeric", month: "short" });
function marksLine(copy, several) {
  if (!copy || !several) return "";
  const parts = [];
  if (copy.your_pages) parts.push(`your marks on ${copy.your_pages} page${copy.your_pages === 1 ? "" : "s"}`);
  for (const [name, n] of Object.entries(copy.others ?? {})) parts.push(`${n} by ${name || "no name"}`);
  if (!parts.length) parts.push("no marks");
  if (copy.latest) parts.push(`last ${shortDay(copy.latest)}`);
  let line = `<div class="marks${copy.flag ? " flagged" : ""}">${esc(parts.join(" · "))}`;
  if (copy.flag) line += ` <span title="${esc(copy.flag)}">⚑ ${esc(copy.flag)}</span>
    <button class="act" data-pull="${esc(copy.path)}">bring them into the library copy</button>`;
  return line + `</div>`;
}

async function pullMarks(path) {
  const item = state.items[state.selected];
  if (!item?.work_id) return;
  if (!confirm(`Bring the marks in ${path.split("/").pop()} into the library copy?\n\n` +
               "Only the library copy changes; refs keeps a copy of it as it was. Marks deleted in " +
               "one copy are not deleted in the other.")) return;
  say("bringing the marks over …", 0);
  const answer = await post(`/api/work/${item.work_id}/pull`, { path });
  if (answer.detail) { say(answer.detail, 12000); return; }
  say(answer.added ? `brought ${answer.added} marks into the library copy` +
      (answer.skipped ? ` (${answer.skipped} of a kind refs cannot redraw)` : "") +
      `; the previous version is in ${answer.backup}` : "the library copy has every mark already", 12000);
  renderDetail();
}

// --- reading notes --------------------------------------------------------------------
// The note is a Markdown file in the Obsidian vault (notes.py), so it is also
// there in Obsidian and on the phone. The box saves a moment after you stop
// typing, when you leave it, and on ⌘S. Every save names the version of the
// file it started from; if the file changed in Obsidian meanwhile, the server
// refuses and the box keeps your text until you choose what to do.

const noteSaved = {};               // work id → { version, body } last read or written
const noteConflict = new Set();     // work ids whose last save was refused (changed in Obsidian)
let noteQueue = Promise.resolve();  // one save at a time, so versions follow in order
let noteTimer = null;

function noteHtml(work, data) {
  if (!data.available) {
    return `<div class="label">Notes</div><p class="where">Notes are off: no Obsidian vault is set
      (or it was not found). Set <code>obsidian_vault</code> in ~/.config/refs/config.toml.</p>`;
  }
  return `<div class="label">Notes</div>
    <textarea id="note" class="note" data-work="${esc(work.id)}" spellcheck="true"
      placeholder="Notes on this paper (Markdown). Saved to Obsidian as ${esc(data.file)}">${esc(data.body)}</textarea>
    <div class="acts note-bar">
      <a class="act" href="${esc(data.uri)}">open in obsidian</a>
      <span id="notestate" class="badge">${data.exists ? esc(data.file) : ""}</span>
    </div>`;
}

function noteState(text) { const el = $("notestate"); if (el) el.innerHTML = text; }

function saveNote() {
  const box = $("note");
  clearTimeout(noteTimer);
  if (!box) return noteQueue;
  // Read the text now: by the time the save runs, the box may show another paper.
  const workId = box.dataset.work, body = box.value;
  noteQueue = noteQueue.then(() => sendNote(workId, body)).catch(() => {});
  return noteQueue;
}

async function sendNote(workId, body) {
  const saved = noteSaved[workId];
  // After a refusal, retrying cannot succeed: leave the message (and its
  // button) in place until the note is read again.
  if (!saved || saved.body === body || noteConflict.has(workId)) return;
  const here = () => $("note")?.dataset.work === workId;
  if (here()) noteState("saving …");
  const response = await fetch(`/api/work/${workId}/note`, {
    method: "POST", headers: { "Content-Type": "application/json", "X-Refs": "1" },
    body: JSON.stringify({ body, version: saved.version }),
  });
  const data = await response.json();
  if (!response.ok) {
    if (response.status === 409) {
      noteConflict.add(workId);
      if (here()) noteState(`changed in Obsidian, not saved. <button class="act" data-act="note-reload">discard mine, load Obsidian's</button>`);
    } else say(data.detail ?? "the note was not saved", 8000);
    return;
  }
  noteSaved[workId] = { version: data.note.version, body };
  if (here()) noteState(`saved · ${esc(data.note.file)}`);
}

// Save while typing (after a pause), and when leaving the box.
$("detail").addEventListener("input", (event) => {
  if (event.target.id !== "note" || noteConflict.has(event.target.dataset.work)) return;
  noteState("editing");
  clearTimeout(noteTimer);
  noteTimer = setTimeout(saveNote, 1200);
});
$("detail").addEventListener("focusout", (event) => { if (event.target.id === "note") saveNote(); });

// Coming back from Obsidian: show what was written there, unless the box has
// unsaved text of its own.
window.addEventListener("focus", async () => {
  const box = $("note");
  if (!box || document.activeElement === box) return;
  const workId = box.dataset.work;
  if (box.value !== noteSaved[workId]?.body) return;
  const data = await get(`/api/work/${workId}`);
  if ($("note") !== box || box.value !== noteSaved[workId]?.body) return;
  if (data.note.version !== noteSaved[workId].version) {
    box.value = data.note.body;
    noteSaved[workId] = { version: data.note.version, body: data.note.body };
    noteState(`updated from Obsidian · ${esc(data.note.file)}`);
  }
});

let currentWork = null;

function showEditor() {
  const meta = currentWork?.meta;
  if (!meta) return;
  const authors = (meta.authors ?? []).map((a) => [a.given, a.family].filter(Boolean).join(" ")).join("; ");
  const field = (name, value, hint = "") => `<label><span>${name}</span>
    <input name="${name}" value="${esc(value ?? "")}" placeholder="${esc(hint)}" autocomplete="off" spellcheck="false"></label>`;
  $("editor").innerHTML = `<form class="edit" id="editform">
    ${field("title", meta.title)}
    ${field("authors", authors, "Given Family; Given Family")}
    ${field("year", meta.year)}
    ${field("venue", meta.venue, "journal, book title or series")}
    <label><span>kind</span><select name="kind">${["paper", "book", "chapter"].map((k) =>
      `<option${k === meta.kind ? " selected" : ""}>${k}</option>`).join("")}</select></label>
    ${field("volume", meta.volume)}${field("issue", meta.issue)}${field("pages", meta.pages)}
    ${field("doi", meta.doi, "paste a DOI, then “fetch record”")}
    <div class="acts">
      <button class="act" type="submit">save</button>
      <button class="act" type="button" data-act="fetch-doi">fetch record for this doi</button>
      <button class="act" type="button" data-act="cancel-edit">cancel</button>
    </div></form>`;
  $("editform").title.focus();
}

function parseAuthors(text) {
  // "Given Family; Given Family" or "Family, Given; Family, Given"
  return text.split(";").map((part) => part.trim()).filter(Boolean).map((part) => {
    if (part.includes(",")) {
      const [family, given] = part.split(",").map((x) => x.trim());
      return { given: given ?? "", family };
    }
    const words = part.split(/\s+/);
    return { given: words.slice(0, -1).join(" "), family: words[words.length - 1] };
  });
}

async function changeWork(path, body, message) {
  const id = currentWork.id;
  const response = await fetch(`/api/work/${id}/${path}`, {
    method: "POST", headers: { "Content-Type": "application/json", "X-Refs": "1" },
    body: JSON.stringify(body ?? {}),
  });
  if (!response.ok) { say((await response.json()).detail ?? "that did not work"); return; }
  say(message);
  loadSidebar();
  await search(id);
}

$("detail").addEventListener("submit", (event) => {
  event.preventDefault();
  if (event.target.id === "newcoll") { createCollection(event.target); return; }
  if (event.target.id !== "editform") return;
  const form = new FormData(event.target), fields = {};
  for (const [name, value] of form) fields[name] = String(value).trim();
  fields.authors = parseAuthors(fields.authors);
  if (!fields.authors.length) delete fields.authors;
  if (fields.kind === "paper") fields.type = fields.venue ? "journal-article" : "posted-content";
  changeWork("edit", { fields }, "saved your corrections");
});

$("detail").addEventListener("click", (event) => {
  const el = event.target;
  if (el.dataset.identify) identify(el.dataset.identify);
  else if (el.dataset.open) post("/api/open", { path: el.dataset.open });
  else if (el.dataset.reveal) post("/api/open", { path: el.dataset.reveal, reveal: true });
  else if (el.dataset.fileto) openPicker([el.dataset.fileto], "move");
  else if (el.dataset.copyto) openPicker([el.dataset.copyto], "copy");
  else if (el.dataset.batch) batchAct(el.dataset.batch);
  else if (el.closest(".choice")) {
    const choice = el.closest(".choice");
    if (el.dataset.inside) pickerInside(choice.dataset.folder);
    else pickerChoose(choice.dataset.folder, choice.dataset.create === "1");
  }
  else if (el.dataset.dl) takeDownloads(el.dataset.dl);
  else if (el.dataset.inbox) takeInbox();
  else if (el.dataset.mig) migrateAct(el.dataset.mig);
  else if (el.dataset.pull) pullMarks(el.dataset.pull);
  else if (el.dataset.collAdd) openCollectionPicker([state.items[state.selected]?.work_id].filter(Boolean), $("collpicker"));
  else if (el.dataset.collOut) collect([state.items[state.selected]?.work_id].filter(Boolean), el.dataset.collOut, false);
  else if (el.dataset.collShow) {
    state.collection = el.dataset.collShow; state.context = null; state.tag = null; state.view = "all";
    markSidebar(); search();
  }
  else if (el.closest(".opt")) collect(cpick.ids, el.closest(".opt").dataset.coll);
  else if (el.dataset.act === "cancel-new") renderDetail();
  else if (el.dataset.act === "edit") showEditor();
  else if (el.dataset.act === "cancel-edit") $("editor").innerHTML = "";
  else if (el.dataset.act === "note-reload") renderDetail();
  else if (el.dataset.act === "confirm") changeWork("confirm", {}, "accepted as is");
  else if (el.dataset.act === "revert") changeWork("edit", { fields: {} }, "back to the automatic record");
  else if (el.dataset.act === "fetch-doi") {
    const doi = $("editform").doi.value.trim();
    if (doi) changeWork("doi", { doi }, "record replaced from the registry");
    else say("type a DOI in the doi field first");
  }
  else if (el.dataset.act) act(el.dataset.act);
  else if (el.dataset.tag) {
    state.tag = el.dataset.tag; state.context = null; state.collection = null; state.view = "all";
    markSidebar(); search();
  }
});

// --- actions (shared by keys and buttons) -------------------------------------------

// Read a PDF that is on disk but was never read (about $0.001).
function identify(path) {
  const name = path.split("/").pop();
  say(`adding ${name} …`, 0);
  follow(post("/api/identify", { path }), name);
}

// One copy of a paper to the macOS Trash (the server refuses the last copy).
async function trashCopy(path) {
  if (!confirm(`Move this copy to the Trash?\n\n${path}`)) return;
  const response = await fetch("/api/trash", { method: "POST",
    headers: { "Content-Type": "application/json", "X-Refs": "1" },
    body: JSON.stringify({ path }) });
  say(response.ok ? "moved to the Trash" : (await response.json()).detail);
  loadSidebar(); search(state.items[state.selected]?.id ?? null);
}

// Copy text. `text` may be a promise (BibTeX still being fetched): Safari
// allows the clipboard only during the click itself, so the write starts
// now and its contents follow.
async function copyText(text, message) {
  try {
    if (typeof text !== "string" && window.ClipboardItem) {
      const blob = Promise.resolve(text).then((t) => new Blob([t], { type: "text/plain" }));
      await navigator.clipboard.write([new ClipboardItem({ "text/plain": blob })]);
    } else {
      await navigator.clipboard.writeText(await text);
    }
    say(typeof message === "function" ? message(await text) : message ?? "copied");
  } catch {
    say("could not copy: the browser refused the clipboard", 8000);
  }
}

// The BibTeX of these works, one entry after another.
function copyBibtex(ids) {
  const text = Promise.all(ids.map((id) => get(`/api/work/${id}`).then((d) => d.bibtex ?? "", () => "")))
    .then((entries) => entries.filter(Boolean).join("\n"));
  copyText(text, `copied BibTeX for ${ids.length === 1 ? "1 paper" : `${ids.length} papers`}`);
}

async function act(name) {
  const item = state.items[state.selected];
  if (!item) return;
  const path = item.locations[0]?.path;
  if (name === "open" && path) window.open(`/file?path=${encodeURIComponent(path)}`, "_blank");
  if (name === "preview" && path) post("/api/open", { path });
  if (name === "reveal" && path) post("/api/open", { path, reveal: true });
  if (name === "citekey" && item.citekey) {
    await navigator.clipboard.writeText(item.citekey);
    say(`copied ${item.citekey}`);
  }
  if (name === "bibtex" && $("detail").dataset.bibtex) {
    await navigator.clipboard.writeText($("detail").dataset.bibtex);
    say(`copied bibtex for ${item.citekey}`);
  }
  if ((name === "read" || name === "queue") && item.work_id) {
    const done = name === "read"
      ? await post(`/api/work/${item.work_id}/read`, { status: item.read_status === "read" ? "unread" : "read" })
      : await post(`/api/work/${item.work_id}/queue`, { on: !item.queued });
    if (done.detail) { say(done.detail); return; }
    const leftQueue = item.queued && !done.queued;
    Object.assign(item, done);                 // read_status, read_source, queued
    if (name === "queue") say(done.queued ? `queued ${item.citekey}` : `${item.citekey} off the queue`);
    else if (leftQueue) say(`read ${item.citekey}: off the queue`);
    loadSidebar();
    // In the queue view a paper that left the queue leaves the list.
    if ((state.view === "queue" && leftQueue)
        || (["unread", "reading", "read"].includes(state.view) && item.read_status !== state.view)) {
      search(); return;
    }
    const keep = state.selected;
    renderList(); select(keep, false);
  }
}

// --- search bar, including "add" mode -------------------------------------------------

function setAdding(on) {
  state.adding = on;
  $("prompt").textContent = on ? "+" : "/";
  $("q").value = on ? "" : state.query;
  $("q").placeholder = on
    ? "add a paper: a URL, DOI, arXiv id or NBER number, then Enter; #220a puts it in a course (Tab completes)"
    : "search   #220a   author:smith   tag:theory   year:2019";
  if (on) $("q").focus();
}

let typing;
$("q").addEventListener("input", () => {
  if (state.adding) return;
  state.query = $("q").value.trim();
  clearTimeout(typing);
  typing = setTimeout(search, 120);        // wait for a pause in typing
});

$("q").addEventListener("keydown", async (event) => {
  if (event.key === "Escape") {
    if (state.adding) setAdding(false);
    else if ($("q").value) { $("q").value = ""; state.query = ""; search(); }
    $("q").blur();
  }
  if (event.key === "Enter") {
    if (!state.adding) {
      clearTimeout(typing);
      $("q").blur();
      if (state.query) search(null, "auto");
      return;
    }
    // #names are collections to put the paper in; the rest is what to add.
    const words = $("q").value.trim().split(/\s+/).filter(Boolean);
    const collections = words.filter((w) => w.length > 1 && w.startsWith("#")).map((w) => w.slice(1));
    const thing = words.filter((w) => !w.startsWith("#")).join(" ");
    if (!thing) return;
    const queue = event.shiftKey;
    setAdding(false); $("q").blur();
    say(`adding ${thing}${queue ? " to the queue" : ""}${collections.length ? " into #" + collections.join(" #") : ""} …`, 0);
    follow(post("/api/add", { thing, queue, collections }), thing);
  }
  // Tab after "#22" completes a collection: this term's courses first.
  if (event.key === "Tab" && state.adding) {
    const value = $("q").value, fragment = value.match(/#([^\s#]*)$/)?.[1];
    if (fragment == null) return;
    event.preventDefault();
    collectionList ??= (await get("/api/collections")).collections;
    const hits = collectionOrder(collectionList).filter((c) => c.name.startsWith(fragment.toLowerCase()));
    if (!hits.length) { say(`no collection starts with ${fragment}; a new name makes one`); return; }
    $("q").value = value.slice(0, value.length - fragment.length) + hits[0].name + " ";
    if (hits.length > 1) say(`also: ${hits.slice(1, 6).map((c) => "#" + c.name).join(" ")}`);
  }
  if (event.key === "ArrowDown") { event.preventDefault(); $("q").blur(); }
});

// --- following an "add" job -----------------------------------------------------------

// Adding runs in the background on the server. Show elapsed time while it
// runs, the outcome when it ends, and say so plainly if contact is lost.
async function follow(start, label) {
  try {
    const begun = await start;
    if (begun.message) { say(begun.message.split("\n")[0], 9000); return; }   // e.g. already in the library
    if (!begun.job) { say(begun.detail ?? "could not start", 9000); return; }
    for (;;) {
      await new Promise((resolve) => setTimeout(resolve, 1500));
      const job = await get(`/api/job/${begun.job}`);
      if (job.state === "running") {
        say(`adding ${label} … ${job.message ? `${job.message} · ` : ""}${job.seconds}s`, 0);
        continue;
      }
      say((job.state === "failed" ? "failed: " : "") + (job.message.split("\n")[0] || job.state), 12000);
      break;
    }
  } catch {
    say(`lost contact with refs while adding ${label}. The file, if saved, is in ${state.dump}.`, 0);
  }
  loadSidebar(); search();
}

// --- adding by drop or paste ---------------------------------------------------------

$("addbutton").addEventListener("click", () => setAdding(true));

// The brand, top left: back to all papers, with the search box emptied (the
// row selection for a batch stays). A ⌘-click still opens a new tab.
function goHome() {
  clearTimeout(typing);
  state.query = ""; state.collection = null; state.context = null; state.tag = null; state.view = "all";
  setAdding(false);
  markSidebar();
  search();
}
$("brand").addEventListener("click", (event) => {
  if (event.metaKey || event.ctrlKey || event.shiftKey) return;
  event.preventDefault();
  goHome();
});

// A link, DOI, arXiv id or NBER number pasted anywhere (outside a text field)
// opens the add bar with it filled in; Enter confirms.
const LOOKS_ADDABLE = /^(https?:\/\/\S+|10\.\d{4,9}\/\S+|(arxiv:)?\d{4}\.\d{4,5}(v\d+)?|w\d{4,5})$/i;
document.addEventListener("paste", (event) => {
  if (event.target.matches?.("input, textarea, select")) return;
  const text = (event.clipboardData?.getData("text") ?? "").trim();
  if (!LOOKS_ADDABLE.test(text)) return;
  event.preventDefault();
  setAdding(true);
  $("q").value = text;
  say("press Enter to add it");
});

// Dragging files over the window (as opposed to dragging a column header).
const draggingFiles = (event) => [...(event.dataTransfer?.types ?? [])].includes("Files");
window.addEventListener("dragover", (event) => {
  if (!draggingFiles(event)) return;
  event.preventDefault();
  document.body.classList.add("dropping");
});
window.addEventListener("dragleave", (event) => {
  if (!event.relatedTarget) document.body.classList.remove("dropping");
});
window.addEventListener("drop", async (event) => {
  if (!draggingFiles(event)) return;
  event.preventDefault();
  document.body.classList.remove("dropping");
  const files = [...event.dataTransfer.files].filter((f) => f.name.toLowerCase().endsWith(".pdf"));
  if (!files.length) { say("only PDF files can be added"); return; }
  for (const file of files) {
    say(`adding ${file.name} …`, 0);
    const into = state.collection ? `&to=${encodeURIComponent(state.collection)}` : "";
    const upload = fetch(`/api/upload?name=${encodeURIComponent(file.name)}${into}`, {
      method: "POST", headers: { "X-Refs": "1", "Content-Type": "application/pdf" }, body: file,
    }).then((response) => response.json());
    await follow(upload, file.name);       // one at a time, so progress stays readable
  }
});

// --- review: proposed renames ----------------------------------------------------------

// Each file whose name differs from the house convention (outside protected
// folders such as canvas-sync, overleaf and Git repositories). Nothing is
// renamed until you tick it and press "rename selected".
async function showRenames(token) {
  const data = await get("/api/renames");
  if (token !== listToken) return;
  state.items = [];
  const props = data.proposals;
  const known = new Set(props.map((p) => p.path));
  for (const path of [...state.picked]) if (!known.has(path)) state.picked.delete(path);
  $("count").textContent = `${props.length} renames`;

  let html = `<div class="bar">
    <button class="act" data-pick="all">select all</button>
    <button class="act" data-pick="none">select none</button>
    <button class="act" data-apply="1">rename selected (<span id="npicked">${state.picked.size}</span>)</button>
    ${state.lastBatch ? `<button class="act" data-undo="${state.lastBatch}">undo the last ${state.lastBatch}</button>` : ""}
  </div>`;
  if (!props.length) html += `<div class="empty">every file already has its proper name.</div>`;
  let folder = null;
  for (const p of props) {
    if (p.folder !== folder) {
      folder = p.folder;
      html += `<div class="topic folder"><span>${esc(folder)}</span>
        <button class="act" data-pick-folder="${esc(folder)}">select folder</button></div>`;
    }
    const who = `${authorsShort(p.authors)} (${p.year ?? "n.d."}) ${p.title ?? ""}`;
    html += `<label class="prop"><input type="checkbox" data-path="${esc(p.path)}"${state.picked.has(p.path) ? " checked" : ""}>
      <span class="old" title="${esc(p.path.split("/").pop())}">${esc(p.path.split("/").pop())}</span>
      <span class="new" title="${esc(p.new_name)}">→ ${esc(p.new_name)}</span>
      <span class="who">${esc(who)}</span></label>`;
  }
  $("list").innerHTML = html;
  $("detail").innerHTML = `<div class="label">Renames</div>
    <p>Names built from the verified record: <code>surname-year-short-title.pdf</code>.
    Files stay in their folders. Your old names stay searchable.</p>
    <p class="where" style="margin-top:8px">${data.protected} more are in folders refs never renames
    (folders another tool keeps in sync, set under settings, and Git repositories).</p>
    <p class="where" style="margin-top:8px">A file in a folder you share with coauthors is
    renamed for them too, through the shared folder.</p>`;
}

async function applyRenames() {
  const paths = [...state.picked];
  if (!paths.length) { say("tick the files to rename first"); return; }
  say(`renaming ${paths.length} …`, 0);
  const result = await post("/api/renames/apply", { paths });
  state.picked.clear();
  state.lastBatch = result.done ?? 0;
  say(`renamed ${result.done} files`);
  loadSidebar(); search();
}

async function undoBatch(count) {
  const result = await post("/api/undo", { count });
  state.lastBatch = 0;
  state.lastFiled = 0;
  say(`undone: ${result.restored} of ${count}`);
  loadSidebar(); search();
}

// --- review: PDFs in Downloads ----------------------------------------------------------

// The PDFs directly in ~/Downloads (the setting), newest first. The server sorts
// each into paper / maybe / other from what is printed in it (downloads.py), on
// this Mac and for free; papers and maybes are listed, the rest on request.
// Ticked files are moved (or copied) into the quick-add folder or a folder you
// pick, then read as one job. Files refs already has stay where they are.
const DL_KIND = { paper: "paper", maybe: "maybe", other: "not a paper?", unreadable: "unreadable",
  incomplete: "downloading?" };
const fileSize = (bytes) => bytes < 1e6 ? `${Math.max(1, Math.round(bytes / 1e3))} kB` : `${(bytes / 1e6).toFixed(1)} MB`;
const fileDate = (t) => new Date(t * 1000).toLocaleDateString([], { year: "numeric", month: "short", day: "numeric" });

async function showDownloads(token) {
  const data = await get("/api/downloads");
  if (token !== listToken) return;
  state.items = [];
  state.dl = data;
  if (!data.folder) {
    $("count").textContent = "downloads";
    $("list").innerHTML = `<div class="empty">no Downloads folder${data.setting ? ` at ${esc(data.setting)}` : ""}.
      Set one under settings (Library → Downloads folder).</div>`;
    $("detail").innerHTML = "";
    return;
  }
  const papers = (i) => ["paper", "maybe"].includes(i.kind);
  const waiting = data.items.filter((i) => !i.in_refs);
  const shown = data.items.filter((i) => state.dlOthers || papers(i) || i.in_refs);
  const hidden = data.items.length - shown.length;
  const had = data.items.filter((i) => i.in_refs);     // copies refs has: may go to the Trash
  const takeable = new Set(waiting.filter((i) => i.kind !== "incomplete").map((i) => i.name));
  for (const name of [...state.picked]) if (!takeable.has(name)) state.picked.delete(name);
  $("count").textContent = `${waiting.filter(papers).length} papers in Downloads`;
  // The sidebar's count was taken before the server had looked at new files.
  const nav = $("side").querySelector('.nav[data-view="downloads"] .n');
  if (nav) nav.textContent = waiting.filter(papers).length;

  let html = `<div class="bar">
    <button class="act" data-pick="all">select all</button>
    <button class="act" data-pick="none">select none</button>
    <span class="where"><span id="npicked">${state.picked.size}</span> ticked</span>
    <button class="act" data-dl-others="1">${state.dlOthers ? "hide the other PDFs" : `show ${hidden} other PDFs`}</button>
    ${had.length ? `<button class="act" data-dl-trash="*">trash the ${had.length} already in refs</button>` : ""}
  </div>`;
  if (!shown.length) html += `<div class="empty">no PDFs that look like papers in ${esc(data.folder)}.</div>`;
  for (const item of shown) {
    const can = takeable.has(item.name);
    const about = `${fileDate(item.modified)} · ${fileSize(item.size)}${item.pages ? ` · ${item.pages} p.` : ""}`;
    const why = item.in_refs ? `already in refs: ${item.in_refs}` : `${DL_KIND[item.kind] ?? item.kind}: ${item.why}`;
    html += `<label class="prop dl${can ? "" : " off"}"><input type="checkbox" data-path="${esc(item.name)}"${
      state.picked.has(item.name) ? " checked" : ""}${can ? "" : " disabled"}>
      <span class="name" title="${esc(item.name)}">${esc(item.name)}</span>
      <span class="about">${esc(about)}</span>
      <span class="who ${item.kind}" title="${esc(why)}">${esc(why)}</span>${item.in_refs
        ? `<button class="act trash" data-dl-trash="${esc(item.name)}" title="move this copy in Downloads to the Trash">trash</button>` : ""}</label>`;
  }
  if (data.older) html += `<div class="where" style="padding: 4px 2ch">${data.older} older PDFs are not listed.</div>`;
  $("list").innerHTML = html;
  drawDownloadsDetail();
}

// --- the inbox (inbox.py) ----------------------------------------------------------------
// Papers outside the library folder that it lacks: in inbox folders (course
// folders, a course site's sync folder; listed, not indexed) and in shared
// folders (a coauthor's addition). Each row says the collection it would go
// in; ticked rows are taken into the library (moved, or copied from shared and
// managed folders), named like the library's other files.

async function showInbox(token) {
  const data = await get("/api/inbox");
  if (token !== listToken) return;
  state.items = [];
  state.inbox = data;
  const waiting = data.items.filter((i) => !i.in_library);
  const shown = state.inboxHad ? data.items : waiting;
  const takeable = new Set(waiting.map((i) => i.path));
  for (const path of [...state.picked]) if (!takeable.has(path)) state.picked.delete(path);
  $("count").textContent = `${waiting.length} in the inbox`;
  const nav = $("side").querySelector('.nav[data-view="inbox"] .n');
  if (nav) nav.textContent = waiting.length;
  const had = data.items.length - waiting.length;
  let html = `<div class="bar">
    <button class="act" data-pick="all">select all</button>
    <button class="act" data-pick="none">select none</button>
    <span class="where"><span id="npicked">${state.picked.size}</span> ticked</span>
    ${had ? `<button class="act" data-inbox-had="1">${state.inboxHad ? "hide" : "show"} the ${had} already in the library</button>` : ""}
    ${data.unseen ? `<span class="where">looking at ${data.unseen} more PDFs …</span>` : ""}
  </div>`;
  if (!shown.length) {
    html += `<div class="empty">nothing waiting. ${data.inboxes.length ? "" :
      "Inbox folders (course folders, a course site's sync folder) are set under settings."}</div>`;
  }
  let folder = null;
  for (const item of shown) {
    if (item.folder !== folder) {
      folder = item.folder;
      html += `<div class="topic folder"><span>${esc(folder)}</span>
        <button class="act" data-pick-folder="${esc(folder)}">tick all</button></div>`;
    }
    const can = takeable.has(item.path);
    const who = item.title ? `${authorsShort(item.authors)}${item.year ? ` (${item.year})` : ""} ${item.title}` : item.name;
    const into = item.suggested ? `→ #${item.suggested}` : "→ library";
    const how = item.in_library ? "already in the library" : `${item.mode === "copy" ? "copy" : "move"} · ${item.why}`;
    html += `<label class="prop dl${can ? "" : " off"}"><input type="checkbox" data-path="${esc(item.path)}"${
      state.picked.has(item.path) ? " checked" : ""}${can ? "" : " disabled"}>
      <span class="name" title="${esc(item.name)}">${esc(who)}</span>
      <span class="about">${esc(into)}</span>
      <span class="who ${item.kind}" title="${esc(how)}">${esc(how)}</span></label>`;
  }
  $("list").innerHTML = html;
  drawInboxDetail();
}

function drawInboxDetail() {
  $("detail").innerHTML = `<div class="label">Inbox</div>
    <p>Papers the library lacks, in ${state.inbox.inboxes.length ? `your inbox folders (${esc(state.inbox.inboxes.join(", "))})` : "no inbox folders yet"}
    and in folders shared with coauthors. Each goes into the collection shown after the arrow.</p>
    <div class="acts" style="margin-top:8px">
      <button class="act" data-inbox="take">take into the library (<span class="dln">${state.picked.size}</span>)</button>
    </div>
    <div class="adder" style="margin-top:6px"><input name="inboxinto" value="${esc(state.inboxInto ?? "")}"
      placeholder="and also put them in: #bets (optional)" spellcheck="false" autocomplete="off"></div>
    <p class="where" style="margin-top:8px">A PDF is <b>moved</b> into the library, except from a folder shared with
    coauthors or managed by another tool (a course site's sync folder): those are <b>copied</b>, so the original
    stays. Named like the library's other files when its record is checked. <kbd>u</kbd> undoes.
    A PDF refs never read is read first (about $0.001), then filed.</p>`;
  $("detail").querySelector("input[name=inboxinto]").addEventListener("input", (event) => {
    state.inboxInto = event.target.value;
  });
}

async function takeInbox() {
  const byPath = Object.fromEntries((state.inbox?.items ?? []).map((i) => [i.path, i]));
  const choices = [...state.picked].filter((p) => byPath[p])
    .map((path) => ({ path, collections: byPath[path].suggested ? [byPath[path].suggested] : [] }));
  if (!choices.length) { say("tick the papers to take in first"); return; }
  const extra = (state.inboxInto ?? "").split(/[\s,]+/).map((w) => w.replace(/^#/, "")).filter(Boolean);
  say(`taking ${choices.length} into the library …`, 0);
  const answer = await post("/api/inbox/take", { choices, extra });
  if (answer.detail) { say(answer.detail, 9000); return; }
  state.picked.clear();
  state.lastFiled = answer.taken.length;           // u undoes them (the journal)
  const parts = [];
  if (answer.taken.length) parts.push(`took ${answer.taken.length} into the library`);
  if (answer.refused.length) parts.push(`${answer.refused.length} not taken (${answer.refused[0].why})`);
  say(parts.join("; ") + (answer.jobs.length ? "; reading the new ones …" : "") + ". u: undo", 12000);
  loadSidebar(); search();
  for (const job of answer.jobs) await follow(Promise.resolve({ job }), "the new PDFs");
}

// --- the migration (migrate.py) ---------------------------------------------------------------
// Every paper with no PDF in the library folder gets one (moved; copied out of
// shared and managed folders), and keeps its courses and projects as
// collections (readings/<topic> folders as parts). Reviewed here: untick
// what should stay; one batch, undone as one. Then "finish" switches the
// watched folders to the library alone.

async function showMigrate(token) {
  const data = await get("/api/migrate");
  if (token !== listToken) return;
  state.items = [];
  state.migrate = data;
  if (!state.migrateSeen) {                       // first look: everything ticked
    state.picked = new Set(data.rows.map((r) => r.work_id));
    state.migrateSeen = true;
  }
  const ids = new Set(data.rows.map((r) => r.work_id));
  for (const id of [...state.picked]) if (!ids.has(id)) state.picked.delete(id);
  const moving = data.rows.filter((r) => r.source);
  $("count").textContent = `${moving.length} to bring in · ${data.rows.length - moving.length} to file`;
  let html = `<div class="bar">
    <button class="act" data-pick="all">select all</button>
    <button class="act" data-pick="none">select none</button>
    <span class="where"><span id="npicked">${state.picked.size}</span> ticked</span>
  </div>`;
  if (!data.rows.length) html += `<div class="empty">every paper is in the library folder with its collections.</div>`;
  let group = null;
  for (const row of data.rows) {
    if (row.group !== group) {
      group = row.group;
      html += `<div class="topic folder"><span>${esc(group)}</span></div>`;
    }
    const what = row.source ? `${row.mode} ${row.source.split("/").pop()}${row.mode === "copy" ? ` (${row.why})` : ""}`
                            : "already in the library";
    const into = row.collections.length ? row.collections.map((n) => "#" + n).join(" ") : "no new collection";
    html += `<label class="prop dl"><input type="checkbox" data-path="${esc(row.work_id)}"${
      state.picked.has(row.work_id) ? " checked" : ""}>
      <span class="name" title="${esc(row.citekey)}">${esc(row.title)}</span>
      <span class="about" title="${esc(row.source ?? "")}">${esc(what)}</span>
      <span class="who" title="${esc(into)}">→ ${esc(into)}</span></label>`;
  }
  $("list").innerHTML = html;
  drawMigrateDetail();
}

function drawMigrateDetail() {
  const data = state.migrate, moving = data.rows.filter((r) => r.source);
  const copies = moving.filter((r) => r.mode === "copy").length;
  const f = data.finish;
  const n = (count, word) => `${count} ${word}${count === 1 ? "" : "s"}`;
  $("detail").innerHTML = `<div class="label">Into the library</div>
    <p>${n(moving.length, "paper")} without a PDF in <code>${esc(data.dump)}</code>: ${moving.length - copies} moved there,
    ${copies} copied (from folders shared with coauthors, or managed by another tool, which keep theirs).
    Each keeps its courses and projects as collections, its readings/&lt;topic&gt; folder as a part.
    ${n(data.rows.length - moving.length, "paper")} already there just get their collections.</p>
    <div class="acts" style="margin-top:8px">
      <button class="act" data-mig="apply">bring the ticked papers in (<span class="dln">${state.picked.size}</span>)</button>
      ${data.undo ? `<button class="act" data-mig="undo">undo the last batch</button>` : ""}
    </div>
    <div class="dlopts"><label><input type="checkbox" name="readingsmd" checked> write readings.md into each course folder</label></div>
    ${data.unidentified.length ? `<p class="where">${n(data.unidentified.length, "PDF")} that look like papers were never read
      and stay where they are; identify them first (they are under "not identified").</p>` : ""}
    <div class="label" style="margin-top:14px">Then: finish</div>
    <p>Once the papers are in, refs can watch the library alone:</p>
    <dl class="meta">
      <dt>library</dt><dd><input name="libname" value="${esc(f.dump)}" spellcheck="false" autocomplete="off"> (now ${esc(data.dump)})</dd>
      <dt>watched</dt><dd>the library, plus folders shared with coauthors (now ${esc(data.roots.join(", "))})</dd>
      <dt>inboxes</dt><dd>${esc(f.inboxes.join(", ") || "none")}: listed in the Inbox, not indexed</dd>
    </dl>
    <p class="where">Course material (slides, problem sets) stays in its folders and leaves the index. Undo the
    batch above before finishing, not after.</p>
    <div class="acts"><button class="act" data-mig="finish">finish: switch the watched folders</button></div>`;
}

async function migrateAct(name) {
  if (name === "apply") {
    const ids = [...state.picked];
    if (!ids.length) { say("tick the papers to bring in first"); return; }
    const readingsMd = $("detail").querySelector("input[name=readingsmd]")?.checked ?? true;
    say(`bringing ${ids.length} papers into the library …`, 0);
    const answer = await post("/api/migrate/apply", { work_ids: ids, readings_md: readingsMd });
    if (answer.detail) { say(answer.detail, 9000); return; }
    say(`done: ${answer.done} papers (${answer.moved} files into the library)` +
        (answer.refused.length ? `; ${answer.refused.length} refused (${answer.refused[0].why})` : ""), 12000);
  } else if (name === "undo") {
    if (!confirm("Put the last batch back: files to where they were, copies to the Trash, memberships undone?")) return;
    const answer = await post("/api/migrate/undo", {});
    say(`put back ${answer.files} files; ${answer.papers} papers' collections undone`, 9000);
  } else if (name === "finish") {
    const libname = $("detail").querySelector("input[name=libname]")?.value.trim();
    const f = state.migrate.finish;
    if (!confirm(`Rename ${state.migrate.dump} to ${libname}, watch only it (and shared folders), and list ` +
                 `${f.inboxes.join(", ") || "no folders"} as inboxes?\n\nThe previous settings are kept as config.toml.bak.`)) return;
    const answer = await post("/api/migrate/finish", { library_name: libname });
    if (answer.detail) { say(answer.detail, 12000); return; }
    say(`finished: the library is ${answer.settings.dump}; watching it alone`, 12000);
  }
  folderList = null;
  loadSidebar(); search();
}

function drawDownloadsDetail() {
  const data = state.dl;
  const copy = state.dlMode === "copy";
  $("detail").innerHTML = `<div class="label">Downloads</div>
    <p>PDFs saved in <code>${esc(data.where)}</code>, newest first. Tick the ones to bring into refs.</p>
    <div class="acts" style="margin-top:8px">
      <button class="act" data-dl="dump">take into ${esc(data.dump)} (<span class="dln">${state.picked.size}</span>)</button>
      <button class="act" data-dl="folder">take into folder…</button>
    </div>
    <div class="dlopts">
      <label><input type="radio" name="dlmode" value="move"${copy ? "" : " checked"}> move out of Downloads</label>
      <label><input type="radio" name="dlmode" value="copy"${copy ? " checked" : ""}> copy (the original stays)</label>
      <label><input type="checkbox" name="dlqueue"${state.dlQueue ? " checked" : ""}> also queue them</label>
    </div>
    <div class="adder" style="margin-top:6px"><input name="dlinto" value="${esc(state.dlInto ?? "")}"
      placeholder="and put them in: #220a #bets (optional)" spellcheck="false" autocomplete="off"></div>
    <div class="picker" id="dlpicker"></div>
    <p class="where" style="margin-top:8px">Each one is then read like any new paper (about $0.001 a PDF).
    A PDF refs already has, byte for byte, is left in Downloads.</p>
    <p class="where" style="margin-top:8px"><b>paper</b> and <b>maybe</b> come from what is printed in
    the PDF (an identifier, an abstract, references, its length), read on this Mac; nothing is sent
    anywhere before you take a file in. Receipts, tickets and statements are under
    <b>show other PDFs</b>.</p>`;
  for (const input of $("detail").querySelectorAll("input[name=dlmode]")) {
    input.addEventListener("change", () => { state.dlMode = input.value; });
  }
  $("detail").querySelector("input[name=dlqueue]").addEventListener("change", (event) => {
    state.dlQueue = event.target.checked;
  });
  $("detail").querySelector("input[name=dlinto]").addEventListener("input", (event) => {
    state.dlInto = event.target.value;
  });
}

function dlCount() {
  for (const el of $("detail").querySelectorAll(".dln")) el.textContent = state.picked.size;
}

async function takeDownloads(where) {
  const names = [...state.picked];
  if (!names.length) { say("tick the PDFs to take in first"); return; }
  if (where === "folder") {
    openPicker(names, state.dlMode, $("dlpicker"), (folder, create) => sendDownloads(names, folder, create));
    return;
  }
  sendDownloads(names, null, false);
}

async function sendDownloads(names, folder, create) {
  const copying = state.dlMode === "copy";
  say(`${copying ? "copying" : "moving"} ${names.length} PDFs …`, 0);
  const response = await fetch("/api/downloads/take", { method: "POST",
    headers: { "Content-Type": "application/json", "X-Refs": "1" },
    body: JSON.stringify({ names, folder, create, mode: state.dlMode, queue: state.dlQueue,
                           collections: (state.dlInto ?? "").split(/[\s,]+/).map((w) => w.replace(/^#/, "")).filter(Boolean) }) });
  const result = await response.json();
  if (!response.ok) { say(result.detail ?? "could not take them in", 9000); return; }
  folderList = null;                          // a new folder may have been made
  picker.box?.replaceChildren();
  state.picked.clear();
  const { taken, known, refused } = result;
  const parts = [];
  if (taken.length) parts.push(`${copying ? "copied" : "moved"} ${taken.length} into ${result.folder}`);
  if (known.length) parts.push(`${known.length} already in refs (left in Downloads)`);
  if (refused.length) parts.push(`${refused.length} not taken (${refused[0].name}: ${refused[0].why})`);
  search();                                   // the list without what left
  if (!result.job) { say(parts.join("; ") || "nothing taken in", 12000); loadSidebar(); return; }
  say(parts.join("; ") + "; reading …", 0);
  await follow(Promise.resolve({ job: result.job }), taken.length === 1 ? taken[0].to.split("/").pop()
    : `${taken.length} PDFs`);
}

// Downloads copies of PDFs refs already has, byte for byte (say, after "copy"
// took them in): to the macOS Trash, from where they can be dragged back. The
// server hashes each file again and refuses any that refs does not have.
async function trashDownloads(which) {
  const names = which === "*" ? state.dl.items.filter((i) => i.in_refs).map((i) => i.name) : [which];
  if (!names.length) return;
  const what = names.length === 1 ? names[0] : `these ${names.length} PDFs`;
  if (!confirm(`Move ${what} from Downloads to the Trash?\n\nrefs has the same file${
    names.length === 1 ? "" : "s"}, so nothing is lost; drag back from the Trash to undo.`)) return;
  const response = await fetch("/api/downloads/trash", { method: "POST",
    headers: { "Content-Type": "application/json", "X-Refs": "1" }, body: JSON.stringify({ names }) });
  const result = await response.json();
  if (!response.ok) { say(result.detail ?? "could not move them to the Trash", 9000); return; }
  const left = result.refused.length ? `; ${result.refused.length} kept (${result.refused[0].name}: ${result.refused[0].why})` : "";
  say(`moved ${result.trashed.length} to the Trash${left}`, 9000);
  search();
}

// --- review: identical copies -------------------------------------------------------------

// The same PDF in more than one place. Often on purpose (a reading for a course
// and for a project); otherwise trash the extra copy. The last copy cannot be trashed.
// Sets marked "keep all" are listed at the bottom, folded, where they can be un-kept.
// A set of copies with refs' recommendation (keep.py): which copy to keep (★)
// and, for each other one, trash (nothing is lost), trash? (another version
// you may still want), merge (has marks the kept copy lacks) or keep.
const COPY_ACTION = { "": "★ keep", keep: "keep", trash: "trash", "trash?": "trash?", merge: "merge" };
function copyMarks(copy) {
  const parts = [`${copy.pages} p.`];
  if (copy.your_pages) parts.push(`your marks on ${copy.your_pages} p.`);
  for (const [name, n] of Object.entries(copy.others ?? {})) parts.push(`${n} by ${name || "no name"}`);
  if (copy.latest) parts.push(`last ${shortDay(copy.latest)}`);
  return parts.join(" · ");
}
function copySet(item, kept) {
  const name = item.title ?? item.locations[0].path.split("/").pop();
  const who = item.authors?.length ? `${authorsShort(item.authors)} (${item.year ?? "n.d."}) ` : "";
  const advice = item.advice;
  const doable = advice && ["safe", "optional"].includes(advice.verdict);
  const buttons = kept ? `<button class="act" data-unkeep="${esc(item.hash)}">un-keep</button>`
    : (doable ? `<button class="act" data-apply-copies="${esc(item.work_id)}" title="trash what it says, then tidy the name">do it</button>` : "")
      + `<button class="act" data-keep="${esc(item.hash)}">keep all</button>`;
  let html = `<div class="copyset ${advice ? "v-" + advice.verdict : ""}"><div class="topic"><span>${esc(who + name)}</span>${buttons}</div>`;
  if (advice?.summary) {
    html += `<div class="advice">${esc(advice.summary)}${advice.rename_to
      ? ` Then the kept copy takes the name <span class="path">${esc(advice.rename_to)}</span>.` : ""}</div>`;
  }
  const copies = advice?.copies?.length ? advice.copies : item.locations.map((loc) => ({ path: loc.path }));
  for (const copy of copies) {
    const action = advice ? (copy.path === advice.keep ? "" : copy.action) : null;
    const folder = copy.path.slice(0, copy.path.lastIndexOf("/"));
    html += `<div class="copy">
      ${action != null ? `<span class="do do-${(action || "kept").replace("?", "q")}">${esc(COPY_ACTION[action] ?? action)}</span>` : ""}
      <span class="what"><span class="path">${esc(copy.path.split("/").pop())}</span>
        <span class="where">${esc(folder)}${copy.pages != null ? " · " + esc(copyMarks(copy)) : ""}</span>
        ${copy.why && action ? `<span class="why">${esc(copy.why)}</span>` : ""}</span>
      <span class="acts">
        ${action === "merge" ? `<button class="act" data-merge="${esc(copy.path)}" data-into="${esc(advice.keep)}"
            data-work="${esc(item.work_id)}">bring its marks over</button>` : ""}
        <a class="act" href="/file?path=${encodeURIComponent(copy.path)}" target="_blank">open</a>
        <button class="act" data-reveal="${esc(copy.path)}">finder</button>
        ${kept ? "" : `<button class="act" data-trash="${esc(copy.path)}">trash</button>`}</span></div>`;
  }
  return html + `</div>`;
}

async function showCopies(token) {
  const data = await get("/api/copies");
  if (token !== listToken) return;
  state.items = [];
  $("count").textContent = `${data.items.length} sets of copies`;
  state.copies = data.items;
  // Sets where nothing is lost (the same file, copies without marks of their
  // own) can be cleared in one go; the rest want a look first.
  const safe = data.items.filter((i) => i.advice?.verdict === "safe");
  let html = data.items.length ? `<div class="bar">
    ${safe.length ? `<button class="act" data-apply-safe="1" title="the sets where nothing is lost: the same file, or copies without marks of their own">clear the ${safe.length} safe sets</button>` : ""}
    <span class="where">${data.items.length - safe.length} to look at</span></div>`
    : `<div class="empty">no file is stored twice.</div>`;
  const rank = { merge: 0, optional: 1, keep: 2, safe: 3 };
  const ordered = [...data.items].sort((a, b) => (rank[a.advice?.verdict] ?? 9) - (rank[b.advice?.verdict] ?? 9));
  html += ordered.map((item) => copySet(item, false)).join("");
  if (data.kept.length) {
    html += `<details class="kept"><summary class="topic">kept on purpose (${data.kept.length})</summary>
      ${data.kept.map((item) => copySet(item, true)).join("")}</details>`;
  }
  $("list").innerHTML = html;
  $("detail").innerHTML = `<div class="label">Copies</div>
    <p>A paper stored more than once: the same file twice, a copy you marked up next to a clean one,
    a working paper next to the published article. For each, refs says which copy to keep (★) and why:</p>
    <dl class="meta">
      <dt>your marks</dt><dd>the copy you highlighted is kept; one whose marks are all in it can go</dd>
      <dt>the version</dt><dd>the published article over the working paper, a later draft over an earlier one: only suggested</dd>
      <dt>place, name</dt><dd>the library copy, the tidy name, no "-2"</dd>
    </dl>
    <p class="where" style="margin-top:8px"><b>trash</b>: nothing is lost. <b>trash?</b>: another version you may
    still want (a working paper can carry the appendix). <b>merge</b>: it has marks the kept copy lacks;
    <b>bring its marks over</b> first. <b>do it</b> does what the set says, then gives the kept copy its tidy
    name. Everything goes to the macOS Trash, from where it can be dragged back. <b>keep all</b> moves a set
    to <b>kept on purpose</b> at the bottom, where <b>un-keep</b> brings it back.</p>`;
}

// --- settings ----------------------------------------------------------------------------

// The settings page edits ~/.config/refs/config.toml through the server
// (settings.py), which checks every change and keeps the file's comments. The
// form is built from the server's list of fields. Changes stay a draft (kept
// while you visit other views) until "save changes"; the server then uses them
// at once and rescans.
const settings = { data: null, draft: {}, error: null, authors: null };
const LIST_KINDS = new Set(["folders", "names", "globs", "rules", "authors"]);

const sValue = (f) => (f.key in settings.draft ? settings.draft[f.key] : f.value);
const sDirty = () => Object.keys(settings.draft).length > 0;
// `redraw` false: a text box or menu changed. Redrawing then would replace the
// button a click is landing on (the change fires as the box loses focus, just
// before the click), so only the save button and the field's mark change.
function sSet(key, value, redraw = true) {
  const field = settings.data.fields.find((f) => f.key === key);
  if (JSON.stringify(value) === JSON.stringify(field.value)) delete settings.draft[key];
  else settings.draft[key] = value;
  if (settings.error?.key === key) settings.error = null;
  if (redraw) return drawSettings();
  const n = Object.keys(settings.draft).length;
  const save = $("list").querySelector("[data-s-save]");
  if (save) { save.disabled = !n; save.textContent = `save changes${n ? ` (${n})` : ""}`; }
  const name = $("list").querySelector(`[data-field="${key}"] .name`);
  if (name) name.innerHTML = esc(field.label) + (key in settings.draft ? `<span class="badge"> · changed</span>` : "");
}

async function showSettings(token) {
  const data = await get("/api/settings");
  if (token !== listToken) return;
  state.items = [];
  $("count").textContent = "settings";
  settings.data = data;
  drawSettings();
}

function sField(f) {
  const value = sValue(f);
  const changed = f.key in settings.draft ? `<span class="badge"> · changed</span>` : "";
  const bad = settings.error?.key === f.key ? `<p class="bad">${esc(settings.error.message)}</p>` : "";
  const choose = (kind) => `<button class="act" data-s-choose="${esc(f.key)}" data-kind="${kind}">choose…</button>`;
  let edit = "";

  if (f.kind === "choice") {
    edit = `<select data-s-key="${esc(f.key)}">${f.choices.map((c) =>
      `<option${c === value ? " selected" : ""}>${esc(c)}</option>`).join("")}</select>`;
  } else if (f.kind === "vault") {
    // Vaults Obsidian knows on this Mac, plus whatever is set now.
    const options = [...new Set(["", ...settings.data.vaults, value])];
    edit = `<select data-s-key="${esc(f.key)}">${options.map((v) =>
      `<option value="${esc(v)}"${v === value ? " selected" : ""}>${v ? esc(v) : "none: notes are off"}</option>`).join("")}</select>
      ${choose("vault")}`;
  } else if (LIST_KINDS.has(f.kind)) {
    const chips = f.builtin.map((v) => `<span class="chip fixed" title="built in: always on">${esc(v)}</span>`);
    value.forEach((v, i) => chips.push(`<span class="chip">${v === "" ? "<i>(no name)</i>" : esc(v)}<button
      class="act" data-s-remove="${esc(f.key)}" data-index="${i}" title="remove">×</button></span>`));
    edit = `<div class="chips">${chips.join("") || `<span class="where">none</span>`}</div>
      <div class="adder"><input data-s-add="${esc(f.key)}" placeholder="${esc(f.placeholder || "add…")}"
        spellcheck="false" autocomplete="off">
      ${f.kind === "folders" ? choose("folders") : ""}
      ${f.kind === "authors" ? `<button class="act" data-s-authors="1">find names in my PDFs</button>` : ""}</div>`;
    if (f.kind === "authors" && settings.authors) edit += sAuthors(value);
  } else {
    const type = f.kind === "number" ? "number" : "text";
    edit = `<input data-s-key="${esc(f.key)}" type="${type}" value="${esc(value)}"
      placeholder="${esc(f.placeholder)}" spellcheck="false" autocomplete="off"${type === "number" ? ` min="0" step="0.5"` : ""}>
      ${f.kind === "library" ? choose("library") : f.kind === "folder" ? choose("folder") : ""}`;
  }
  return `<div class="setting${bad ? " has-bad" : ""}" data-field="${esc(f.key)}">
    <div class="name">${esc(f.label)}${changed}</div>
    <div class="edit-it">${edit}</div>
    <p class="help">${esc(f.help)}</p>${bad}</div>`;
}

// Names found on highlights in your PDFs: tick the ones that are you.
function sAuthors(value) {
  if (!settings.authors.length) return `<p class="where">no highlights or notes found in your PDFs.</p>`;
  return `<div class="found">${settings.authors.slice(0, 12).map((a) =>
    `<label><input type="checkbox" data-s-author="${esc(a.name)}"${value.includes(a.name) ? " checked" : ""}>
      ${a.name ? esc(a.name) : "<i>(no name)</i>"} <span class="badge">· ${a.files} files, ${a.marks} marks</span></label>`).join("")}</div>`;
}

function drawSettings() {
  const data = settings.data;
  if (!data) return;
  if (data.error) {
    $("list").innerHTML = `<div class="empty">The settings file has a mistake: ${esc(data.error)}
      Fix it in a text editor, then reload this page.</div>`;
    return;
  }
  const n = Object.keys(settings.draft).length;
  let html = `<div class="bar">
    <button class="act" data-s-save="1"${n ? "" : " disabled"}>save changes${n ? ` (${n})` : ""}</button>
    ${n ? `<button class="act" data-s-discard="1">discard</button>` : ""}
    ${settings.error && !settings.error.key ? `<span class="bad">${esc(settings.error.message)}</span>` : ""}
  </div>`;
  if (data.not_set_up) html += `<p class="setup">${esc(data.not_set_up)}</p>`;
  let group = null;
  for (const f of data.fields) {
    if (f.group !== group) html += `<div class="topic"><span>${esc(group = f.group)}</span></div>`;
    html += sField(f);
  }
  // Keep the place and the focused box when the form is drawn again.
  const scroll = $("list").scrollTop;
  const focused = document.activeElement?.dataset?.sAdd;
  $("list").innerHTML = `<div class="settings">${html}</div>`;
  $("list").scrollTop = scroll;
  if (focused) $("list").querySelector(`[data-s-add="${focused}"]`)?.focus();

  const key = { set: "stored in the macOS Keychain", environment: "from ANTHROPIC_API_KEY",
                missing: "not set: refs cannot read new PDFs" }[data.api_key];
  $("detail").innerHTML = `<div class="label">Settings</div>
    <p>Saved in <code>${esc(data.file)}</code>. Comments you write there are kept; the previous
    version is kept as <code>config.toml.bak</code>.</p>
    <p class="where" style="margin-top:8px">Saving applies the changes at once and rescans the
    watched folders. Folders are inside the library folder; "choose…" opens the Finder dialog.</p>
    <div class="label">Anthropic API key</div>
    <p class="${data.api_key === "missing" ? "bad" : "where"}">${esc(key)}</p>
    <div class="adder"><input id="s-key" type="password" placeholder="sk-ant-…" autocomplete="off" spellcheck="false">
      <button class="act" data-s-key-save="1">${data.api_key === "missing" ? "save key" : "replace key"}</button></div>
    <p class="where" style="margin-top:6px">refs uses it to read new PDFs (about $0.001 each) and,
    when a search is unsure, to ask (about $0.0005). The key is never shown again or written to a file.</p>`;
}

async function saveSettings() {
  // Something typed into an "add…" box but not yet added (Enter) counts too.
  for (const input of $("list").querySelectorAll("input[data-s-add]")) if (input.value.trim()) sAddTyped(input);
  if (!sDirty()) return;
  say("saving …", 0);
  const response = await fetch("/api/settings", { method: "POST",
    headers: { "Content-Type": "application/json", "X-Refs": "1" },
    body: JSON.stringify({ changes: settings.draft }) });
  const answer = await response.json();
  if (!response.ok) {
    settings.error = { key: answer.key ?? null, message: answer.error ?? "could not save" };
    say(answer.error ?? "could not save", 8000);
    drawSettings();
    $("list").querySelector(".has-bad")?.scrollIntoView({ block: "center" });
    return;
  }
  settings.data = answer;
  settings.draft = {};
  settings.error = null;
  drawSettings();
  say("saved. Rescanning with the new settings …");
  setTimeout(loadSidebar, 1500);
}

function sAddTyped(input) {
  const key = input.dataset.sAdd;
  const text = input.value.trim();
  if (!text) return;
  const field = settings.data.fields.find((f) => f.key === key);
  input.value = "";
  sSet(key, [...sValue(field).filter((v) => v !== text), text]);
}

$("list").addEventListener("click", async (event) => {
  const el = event.target.closest("button");
  if (!el || state.view !== "settings") return;
  const d = el.dataset;
  if (d.sSave) await saveSettings();
  else if (d.sDiscard) { settings.draft = {}; settings.error = null; drawSettings(); }
  else if (d.sRemove) {
    const field = settings.data.fields.find((f) => f.key === d.sRemove);
    sSet(d.sRemove, sValue(field).filter((_, i) => i !== Number(d.index)));
  }
  else if (d.sChoose) {
    const field = settings.data.fields.find((f) => f.key === d.sChoose);
    say("choose a folder in the Finder dialog …", 0);
    const answer = await post("/api/settings/folder", { prompt: `refs: ${field.label}`, kind: d.kind });
    say(answer.error ?? (answer.path ? "" : "nothing chosen"));
    if (!answer.path) return;
    if (d.kind === "folders") sSet(field.key, [...sValue(field).filter((v) => v !== answer.path), answer.path]);
    else sSet(field.key, answer.path);
  }
  else if (d.sAuthors) {
    say("reading the highlights in your PDFs …", 0);
    settings.authors = (await get("/api/settings/authors")).authors;
    say(`${settings.authors.length} names found`);
    drawSettings();
  }
});

$("list").addEventListener("change", (event) => {
  const el = event.target;
  if (state.view !== "settings") return;
  if (el.dataset.sKey) {
    const field = settings.data.fields.find((f) => f.key === el.dataset.sKey);
    sSet(field.key, field.kind === "number" ? Number(el.value) : el.value, false);
  } else if (el.dataset.sAuthor !== undefined) {
    const field = settings.data.fields.find((f) => f.kind === "authors");
    const name = el.dataset.sAuthor;
    const now = sValue(field).filter((v) => v !== name);
    sSet(field.key, el.checked ? [...now, name] : now);
  }
});

$("list").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && event.target.dataset?.sAdd) { event.preventDefault(); sAddTyped(event.target); }
});

$("detail").addEventListener("click", async (event) => {
  if (!event.target.closest("[data-s-key-save]")) return;
  const input = $("s-key");
  const response = await fetch("/api/settings/key", { method: "POST",
    headers: { "Content-Type": "application/json", "X-Refs": "1" },
    body: JSON.stringify({ key: input.value }) });
  const answer = await response.json();
  input.value = "";
  if (!response.ok) { say(answer.error, 8000); return; }
  settings.data.api_key = answer.api_key;
  say("API key saved in the Keychain");
  drawSettings();
});

const REVIEW_VIEWS = { renames: showRenames, copies: showCopies, downloads: showDownloads, inbox: showInbox,
                       migrate: showMigrate, settings: showSettings, sidebar: showSidebarPage };

$("list").addEventListener("change", (event) => {
  const path = event.target.dataset?.path;
  if (!path) return;
  event.target.checked ? state.picked.add(path) : state.picked.delete(path);
  $("npicked").textContent = state.picked.size;
  dlCount();
});

$("list").addEventListener("click", async (event) => {
  const el = event.target.closest("button");
  if (!el) return;
  const boxes = [...$("list").querySelectorAll("input[data-path]:not(:disabled)")];
  const tick = (filter, on) => boxes.filter(filter).forEach((box) => {
    box.checked = on; on ? state.picked.add(box.dataset.path) : state.picked.delete(box.dataset.path);
  });
  if (el.dataset.pick === "all") tick(() => true, true);
  else if (el.dataset.pick === "none") tick(() => true, false);
  else if (el.dataset.pickFolder) {
    const folder = el.dataset.pickFolder;
    const inFolder = (box) => box.dataset.path.slice(0, box.dataset.path.lastIndexOf("/")) === folder;
    tick(inFolder, !boxes.filter(inFolder).every((box) => box.checked));   // toggles the folder
  }
  else if (el.dataset.dlOthers) { state.dlOthers = !state.dlOthers; search(); return; }
  else if (el.dataset.inboxHad) { state.inboxHad = !state.inboxHad; search(); return; }
  else if (el.dataset.dlTrash) { event.preventDefault(); await trashDownloads(el.dataset.dlTrash); return; }
  else if (el.dataset.collDelete) {
    const name = el.dataset.collDelete;
    const answer = await post("/api/collections/delete", { name });
    if (answer.detail) { say(answer.detail, 8000); return; }
    collectionList = null;
    say(`deleted #${answer.name}`);
    state.collection = null; state.view = "all";
    await loadSidebar(); markSidebar(); search();
    return;
  }
  else if (el.dataset.apply) await applyRenames();
  else if (el.dataset.undo) await undoBatch(Number(el.dataset.undo));
  else if (el.dataset.reveal) post("/api/open", { path: el.dataset.reveal, reveal: true });
  else if (el.dataset.keep) {
    await post("/api/copies/keep", { hash: el.dataset.keep });
    say("kept: these copies are on purpose (un-keep under \"kept on purpose\")");
    loadSidebar(); search();
  }
  else if (el.dataset.applyCopies || el.dataset.applySafe) {
    const ids = el.dataset.applySafe
      ? (state.copies ?? []).filter((i) => i.advice?.verdict === "safe").map((i) => i.work_id)
      : [el.dataset.applyCopies];
    // One set: you looked at it, so its "trash?" go too. All safe sets: only "trash".
    const answer = await post("/api/copies/apply", { work_ids: ids, optional: !el.dataset.applySafe });
    if (answer.detail) { say(answer.detail, 9000); return; }
    const parts = [`moved ${answer.trashed.length} ${answer.trashed.length === 1 ? "copy" : "copies"} to the Trash`];
    if (answer.renamed.length) parts.push(`renamed ${answer.renamed.length}`);
    if (answer.refused.length) parts.push(`${answer.refused.length} refused (${answer.refused[0].why})`);
    say(parts.join("; "), 9000);
    loadSidebar(); search();
  }
  else if (el.dataset.merge) {
    if (!confirm("Bring this copy's marks into the kept copy? Only the kept copy changes; refs keeps a copy of it as it was.")) return;
    const answer = await post(`/api/work/${el.dataset.work}/pull`, { path: el.dataset.merge, into: el.dataset.into });
    if (answer.detail) { say(answer.detail, 12000); return; }
    say(`brought ${answer.added} marks into the kept copy; now it can go`, 9000);
    search();
  }
  else if (el.dataset.unkeep) {
    await post("/api/copies/unkeep", { hash: el.dataset.unkeep });
    say("back in the list to review");
    loadSidebar(); search();
  }
  else if (el.dataset.trash) await trashCopy(el.dataset.trash);
  const counter = $("npicked");
  if (counter) counter.textContent = state.picked.size;
  dlCount();
});

// --- file to… / copy to… --------------------------------------------------------------------------------

// A filter box over every folder refs knows (where PDFs already are, and the
// folders above them). Type words of the path; ↑/↓ choose, Enter files.
// A new folder: Tab (or "+" on a folder) fills in that folder's path, then
// type the new name after it; the first choice becomes "new folder …".
// `paths` are one or many PDFs; `mode` is "move" (file to…) or "copy" (copy
// to…: the originals stay put).
let folderList = null;
let picker = { paths: [], mode: "move", box: null, at: 0, onPick: null };

// `onPick(folder, create)`: what choosing a folder does, when it is not filing
// library PDFs (the downloads view takes files in from outside the library).
async function openPicker(paths, mode = "move", box = null, onPick = null) {
  box ??= [...$("detail").querySelectorAll(".picker")].find((el) => el.dataset.for === paths[0]);
  if (!box) return;
  picker = { paths, mode, box, at: 0, onPick };
  // The box takes the keyboard at once, before the folder list has loaded:
  // otherwise quick typing would land on the page's single-key shortcuts.
  const what = paths.length > 1 ? `${paths.length} PDFs` : "";
  const verb = mode === "copy" ? `copy ${what} into` : `file ${what} into`;
  box.innerHTML = `<input class="pick" placeholder="${verb}: type to find a folder; Tab, then a name, for a new one" spellcheck="false" autocomplete="off">
    <div class="choices"><div class="where">loading folders …</div></div>`;
  const input = box.querySelector("input");
  input.focus();
  folderList ??= (await get("/api/folders")).folders;
  input.addEventListener("input", () => { picker.at = 0; drawPicker(); });
  input.addEventListener("keydown", (event) => {
    const choices = [...box.querySelectorAll(".choice")];
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      picker.at = Math.max(0, Math.min(choices.length - 1, picker.at + (event.key === "ArrowDown" ? 1 : -1)));
      drawPicker();
    } else if (event.key === "Tab" && choices[picker.at] && choices[picker.at].dataset.create !== "1") {
      event.preventDefault();
      pickerInside(choices[picker.at].dataset.folder);
    } else if (event.key === "Enter" && choices[picker.at]) {
      const choice = choices[picker.at];
      pickerChoose(choice.dataset.folder, choice.dataset.create === "1");
    }
  });
  drawPicker();
  input.focus();
}

function pickerChoose(folder, create) {
  if (picker.onPick) picker.onPick(folder, create);
  else fileTo(picker.paths, folder, picker.mode, create);
}

// Fill the box with a folder's path and a slash: what follows is a new folder's name.
function pickerInside(folder) {
  const input = picker.box.querySelector("input");
  input.value = folder + "/";
  input.focus();
  picker.at = 0;
  drawPicker();
}

function drawPicker() {
  const box = picker.box, input = box.querySelector("input");
  const typed = input.value.trim();
  const words = typed.toLowerCase().split(/\s+/).filter(Boolean);
  const hits = folderList.filter((f) => words.every((w) => f.toLowerCase().includes(w))).slice(0, 14);
  // A path with a slash that is not a folder yet can be created (the server
  // checks it: inside the scanned folders, not a managed one).
  const asPath = typed.replace(/\/+$/, "");
  const known = new Set(folderList);
  const offerNew = typed.includes("/") && !/\s/.test(asPath) && asPath && !known.has(asPath)
    && !asPath.split("/").some((part) => !part || part.startsWith("."));
  // A first folder every choice shares ("phd/") says nothing: leave it out.
  const first = folderList[0]?.split("/")[0];
  const shared = first && folderList.every((f) => f.startsWith(first + "/")) ? first.length + 1 : 0;
  const rows = [];
  if (offerNew) {
    rows.push(`<div class="choice new" data-folder="${esc(asPath)}" data-create="1">+ new folder ${esc(asPath)}</div>`);
  }
  for (const f of hits) {
    rows.push(`<div class="choice" data-folder="${esc(f)}"><span>${esc(f.slice(shared))}</span>` +
      `<button class="act inside" data-inside="1" title="a new folder inside this one (Tab)">+</button></div>`);
  }
  picker.at = Math.min(picker.at, Math.max(0, rows.length - 1));
  box.querySelector(".choices").innerHTML = rows.map((r, n) =>
    n === picker.at ? r.replace('class="choice', 'class="choice at') : r).join("")
    || `<div class="where">no folder matches${typed.includes("/") ? "" : " (for a new one: Tab on a folder, or type a path with /)"}</div>`;
}

async function fileTo(paths, folder, mode = "move", create = false) {
  const copying = mode === "copy";
  const response = await fetch("/api/file-many", { method: "POST",
    headers: { "Content-Type": "application/json", "X-Refs": "1" },
    body: JSON.stringify({ paths, folder, mode, create }) });
  const result = await response.json();
  if (!response.ok) { say(result.detail ?? (copying ? "could not copy" : "could not move")); return; }
  folderList = null;                          // a new folder, or the new location, may add one
  const done = result.done, refused = result.refused;
  if (!done.length) { say(`nothing ${copying ? "copied" : "filed"}: ${refused[0]?.why ?? "?"}`, 9000); return; }
  // Marked papers that moved now live at their new path (a later copy to… needs it).
  if (!copying) {
    const moved = new Map(done.map((d) => [d.from, d.to]));
    for (const item of state.marked.values()) {
      for (const loc of item.locations) if (moved.has(loc.path)) loc.path = moved.get(loc.path);
    }
  }
  state.lastFiled = done.length;
  const what = done.length === 1 ? done[0].to.split("/").pop() : `${done.length} PDFs`;
  const left = refused.length ? `; ${refused.length} left where they were (${refused[0].why})` : "";
  say(`${copying ? "copied" : "filed"} ${what} into ${result.folder}${left}. u: undo`
      + (copying ? " (trashes the copies)" : ""), 12000);
  picker.box?.replaceChildren();
  loadSidebar();
  search(state.items[state.selected]?.id ?? null);
}

// --- collections (collection.py) -------------------------------------------------------------
// A paper's courses and projects, under "Collections" in the detail pane:
// yours (× takes the paper out) and, dotted, the ones its folder stands for
// (they change only when the file moves). "add to…" (key #) opens a box over
// every collection: the newest term's courses first, then projects, then
// older courses; a new name makes a new collection. The same box takes in a
// whole selection.

let collectionList = null;               // /api/collections, dropped after any change
let cpick = { ids: [], box: null, at: 0 };

function collectionsHtml(item) {
  const own = new Set(item.own_collections ?? []);
  const chips = (item.collections ?? []).map((n) => own.has(n)
    ? `<span class="chip"><span class="tag" data-coll-show="${esc(n)}">#${esc(n)}</span>` +
      `<button class="act" data-coll-out="${esc(n)}" title="take it out of ${esc(n)}">×</button></span>`
    : `<span class="chip fixed" title="its PDF is in ${esc(n)}'s folder: moving the file changes this">` +
      `<span class="tag" data-coll-show="${esc(n)}">#${esc(n)}</span></span>`).join("");
  return `<div class="label">Collections</div><div class="chips">${chips}` +
    `<button class="act" data-coll-add="1" title="key: #">add to…</button></div>` +
    `<div class="picker" id="collpicker"></div>`;
}

function collectionOrder(list) {
  const terms = list.filter((c) => c.kind === "course" && c.term).map((c) => c.term);
  const now = terms.sort((a, b) => termRank(b) - termRank(a))[0];
  // Hidden ones (or parts of a hidden one) last: still there, out of the way.
  const hidden = new Set(list.filter((c) => c.hidden).map((c) => c.name));
  const rank = (c) => (hidden.has(topName(c.name)) ? 3
    : c.kind === "course" && c.term === now ? 0 : c.kind === "course" ? 2 : 1);
  return [...list].sort((a, b) => rank(a) - rank(b)
    || (rank(a) === 2 ? termRank(b.term) - termRank(a.term) : 0) || a.name.localeCompare(b.name));
}

async function openCollectionPicker(ids, box) {
  if (!box || !ids.length) return;
  cpick = { ids, box, at: 0 };
  const what = ids.length > 1 ? `${ids.length} papers` : "it";
  box.innerHTML = `<input class="pick" placeholder="put ${what} in: type a course or project (a new name makes one; 219a/topic for a part)" spellcheck="false" autocomplete="off">
    <div class="choices"><div class="where">loading …</div></div>`;
  const input = box.querySelector("input");
  input.focus();
  collectionList ??= (await get("/api/collections")).collections;
  input.addEventListener("input", () => { cpick.at = 0; drawCollectionPicker(); });
  input.addEventListener("keydown", (event) => {
    const choices = [...box.querySelectorAll(".opt")];
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      cpick.at = Math.max(0, Math.min(choices.length - 1, cpick.at + (event.key === "ArrowDown" ? 1 : -1)));
      drawCollectionPicker();
    } else if (event.key === "Enter" && choices[cpick.at]) {
      event.preventDefault();
      collect(cpick.ids, choices[cpick.at].dataset.coll);
    }
  });
  drawCollectionPicker();
}

const COLLECTION_NAME = /^[a-z0-9][a-z0-9._-]*(\/[a-z0-9][a-z0-9._-]*)*$/;

function drawCollectionPicker() {
  const box = cpick.box, input = box.querySelector("input");
  const typed = input.value.trim().replace(/^#/, "").toLowerCase();
  const words = typed.split(/\s+/).filter(Boolean);
  const hits = collectionOrder(collectionList)
    .filter((c) => words.every((w) => c.name.includes(w))).slice(0, 14);
  const rows = hits.map((c) => {
    const about = [c.kind === "course" ? (c.term ?? "course") : "project", c.shared ? "shared" : "",
                   `${c.works} ${c.works === 1 ? "paper" : "papers"}`].filter(Boolean).join(" · ");
    return `<div class="opt" data-coll="${esc(c.name)}"><span>#${esc(c.name)}</span><span class="badge">${esc(about)}</span></div>`;
  });
  // A name no collection has yet: offered last, so Enter on a near match
  // ("220" while 220a exists) never makes a new collection by accident.
  const fresh = typed.replace(/\s*\/\s*/g, "/").replace(/\s+/g, "-");
  if (fresh && COLLECTION_NAME.test(fresh) && !collectionList.some((c) => c.name === fresh)) {
    rows.push(`<div class="opt new" data-coll="${esc(fresh)}">+ new collection #${esc(fresh)}</div>`);
  }
  cpick.at = Math.min(cpick.at, Math.max(0, rows.length - 1));
  box.querySelector(".choices").innerHTML = rows.map((r, n) =>
    n === cpick.at ? r.replace('class="opt', 'class="opt at') : r).join("")
    || `<div class="where">letters, digits and - only; a / makes a part (219a/public-economics)</div>`;
}

// Put works into a collection (add) or take them out, then update the rows
// in place: no new search, so the list keeps its place.
async function collect(ids, name, add = true) {
  if (!ids.length || !name) return;
  const answer = await post("/api/collections/change", { work_ids: ids, name, add });
  if (answer.detail) { say(answer.detail, 8000); return; }
  collectionList = null;
  for (const item of [...state.items, ...state.marked.values()]) {
    if (answer.works[item.work_id]) Object.assign(item, answer.works[item.work_id]);
  }
  const n = answer.changed.length, papers = n === 1 ? "1 paper" : `${n} papers`;
  const stays = !add && n < ids.length ? " (a paper in it by its folder stays in it)" : "";
  // A shared collection: the copies made for coauthors, or an offer to trash
  // the ones a paper taken out leaves behind (never without asking).
  const copied = answer.copied?.length ? `; copied ${answer.copied.length} into the shared folder` : "";
  const notCopied = answer.not_copied?.length ? `; ${answer.not_copied.length} not copied (${answer.not_copied[0].why})` : "";
  say(add ? (n ? `put ${papers} in #${answer.name}${copied}${notCopied}` : `already in #${answer.name}${copied}${notCopied}`)
          : (n ? `took ${papers} out of #${answer.name}${stays}` : `not taken out of #${answer.name}${stays}`), 8000);
  const left = answer.shared_copies ?? [];
  if (left.length && confirm(`#${answer.name} is shared with coauthors. Move ${left.length === 1 ? "its copy" : `the ${left.length} copies`} ` +
                             `in the shared folder to the Trash?\n\n${left.join("\n")}\n\nCoauthors would no longer see ${left.length === 1 ? "it" : "them"}.`)) {
    let trashed = 0;
    for (const path of left) {
      const done = await post("/api/trash", { path });
      if (!done.detail) trashed += 1;
      else say(`${path.split("/").pop()}: ${done.detail}`, 9000);
    }
    if (trashed) say(`moved ${trashed} shared ${trashed === 1 ? "copy" : "copies"} to the Trash`, 6000);
  }
  cpick.box?.replaceChildren();
  loadSidebar();
  // Taken out of the collection the list shows: it leaves the list.
  if (!add && state.collection && n) search(state.items[state.selected]?.id ?? null);
  else renderDetail();
}

// A new project or course, on the page (`refs project new` in the terminal):
// the "+ new" of the sidebar section it was opened from decides which.
// `prefill` starts the name ("219a/" for a new part). With `edit` (a
// collection from the sidebar), the same form changes that one's details.
function renderNewCollection(kind = "project", prefill = "", edit = null) {
  ++detailToken;
  if (edit) kind = edit.kind;
  const course = kind === "course";
  const field = (name, label, hint = "", value = "") => `<label><span>${label}</span>
    <input name="${name}" placeholder="${esc(hint)}" value="${esc(value ?? "")}" autocomplete="off" spellcheck="false"></label>`;
  const tick = (name, label, on = false) => `<label><span></span><span class="tickline">
    <input type="checkbox" name="${name}"${on ? " checked" : ""}> ${label}</span></label>`;
  const kinds = `<label><span>kind</span><select name="kind">${["project", "course"].map((k) =>
    `<option${k === kind ? " selected" : ""}>${k}</option>`).join("")}</select></label>`;
  $("detail").innerHTML = `<h1>${edit ? `#${esc(edit.name)}` : `New ${kind}`}</h1>
    <p class="where">${edit ? `Its details. Its papers stay as they are; rename it from its menu (right-click it in the sidebar).`
      : `A set of papers, whichever folders their PDFs are in. A paper can be in
    any number of courses and projects; a / makes a part (${course ? "219a/public-economics" : "bets/lit"}).`}</p>
    <form class="edit" id="newcoll">
      ${edit ? `<input type="hidden" name="name" value="${esc(edit.name)}"><input type="hidden" name="edit" value="1">${kinds}`
             : `<input type="hidden" name="kind" value="${kind}">
      ${field("name", "name", course ? "220a, 219a/public-economics" : "bets, thesis", prefill)}`}
      ${course || edit ? field("term", "term", "2026-fall", edit?.term) : ""}
      ${field("folder", "folder", course ? "optional: phd/coursework/2026-fall/220a/readings"
                                         : "optional: phd/research/bets/readings", edit?.folder)}
      ${tick("make_folder", "make the folder")}
      ${tick("shared", "the folder is shared with coauthors", edit?.shared)}
      ${field("bib_file", ".bib", course ? "optional: a .bib for the course"
                                         : "optional: ~/phd/research/bets/overleaf/refs.bib", edit?.bib_file)}
      <div class="acts">
        <button class="act" type="submit">${edit ? "save" : "create"}</button>
        <button class="act" type="button" data-act="cancel-new">cancel</button>
      </div>
      <p class="bad" id="newcollbad"></p>
    </form>`;
  const first = $("newcoll").querySelector(edit ? "input[name=term], input[name=folder]" : "input[name=name]");
  first.focus();
  first.setSelectionRange?.(first.value.length, first.value.length);
}

async function createCollection(form) {
  const f = new FormData(form), editing = f.has("edit");
  // Editing: an emptied field clears that detail (""); a new one leaves it out (null).
  const blank = editing ? "" : null;
  const body = { name: String(f.get("name") ?? "").trim(), kind: f.get("kind"),
                 term: String(f.get("term") ?? "").trim() || blank,
                 folder: String(f.get("folder") ?? "").trim() || blank,
                 bib_file: String(f.get("bib_file") ?? "").trim() || blank,
                 make_folder: f.has("make_folder"), shared: f.has("shared") };
  if (!body.name) { $("newcollbad").textContent = "give it a name"; return; }
  const answer = await post("/api/collections/new", body);
  if (answer.detail) { $("newcollbad").textContent = answer.detail; return; }
  collectionList = null;
  const where = answer.entry.folder && !answer.watched ? " (refs does not watch its folder)" : "";
  say(editing ? `saved #${answer.name}${where}` : `new ${answer.entry.kind} #${answer.name}${where}. Add papers with #`, 8000);
  state.collection = answer.name; state.context = null; state.tag = null; state.view = "all";
  await loadSidebar();
  search();
}

// --- the sidebar page ("sidebar", under refs) -----------------------------------------------
// Every course and project in one table, to pin and hide many at once (and
// bulk: empty ones, past terms' courses), and every section's options: shown,
// folded, sort, what it lists.

async function showSidebarPage(token) {
  state.items = [];
  await loadSidebar();                     // fresh counts and dates
  if (token !== listToken) return;
  drawSidebarPage();
}

const shortDate = (t) => (t ? new Date(t * 1000).toLocaleDateString([], { year: "numeric", month: "short", day: "numeric" }) : "");

function drawSidebarPage() {
  const list = state.sidebar?.collections ?? [];
  const typed = (state.sbFilter ?? "").toLowerCase();
  const kindOf = (c) => state.collectionInfo[topName(c.name)]?.kind ?? c.kind;
  // Projects in the sidebar's order, then courses newest term first; parts under their parent.
  const tops = [...sectionTops("project", list), ...[...sectionTops("course", list)].sort(sorter("term"))];
  const rows = tops.flatMap((t) => [t, ...list.filter((c) => c.name.startsWith(t.name + "/")).sort(byPlace)])
    .filter((c) => !typed || c.name.includes(typed));
  const newest = list.filter((c) => c.kind === "course" && c.term).map((c) => c.term).sort((a, b) => termRank(b) - termRank(a))[0];
  const past = list.filter((c) => !c.name.includes("/") && c.kind === "course" && !c.hidden && c.term && c.term !== newest);
  const empty = list.filter((c) => !c.hidden && !c.works);
  $("count").textContent = `${list.length} collections`;
  let html = `<div class="bar">
    <input class="sbfilter" data-sb-filter="1" value="${esc(state.sbFilter ?? "")}" placeholder="filter" spellcheck="false" autocomplete="off">
    <button class="act" data-sb-bulk="empty" ${empty.length ? "" : "disabled"}>hide the ${empty.length} empty</button>
    <button class="act" data-sb-bulk="past" ${past.length ? "" : "disabled"}
      title="every course of a term before ${esc(newest ?? "the newest")}">hide past terms' courses (${past.length})</button>
    <button class="act" data-sb-bulk="show" ${list.some((c) => c.hidden) ? "" : "disabled"}>show all</button>
  </div>
  <div class="sbrow sbhead"><span>pin</span><span>hide</span><span>name</span><span>kind</span><span class="right">papers</span>
    <span>made</span><span>last added to</span></div>`;
  if (!rows.length) html += `<div class="empty">no collection${typed ? ` matches ${esc(typed)}` : " yet"}.</div>`;
  for (const c of rows) {
    const depth = c.name.split("/").length - 1;
    html += `<div class="sbrow${c.hidden ? " hid" : ""}">
      <span><input type="checkbox" data-sb-pin="${esc(c.name)}"${c.pinned ? " checked" : ""} title="pin to the top of the sidebar"></span>
      <span><input type="checkbox" data-sb-hide="${esc(c.name)}"${c.hidden ? " checked" : ""} title="hide from the sidebar"></span>
      <span class="name" style="padding-left:${2 * depth}ch"><span class="tag" data-coll-show="${esc(c.name)}">${esc(depth ? c.name.split("/").pop() : c.name)}</span></span>
      <span class="dim">${esc(kindOf(c) === "course" ? (state.collectionInfo[topName(c.name)]?.term ?? "course") : "project")}</span>
      <span class="right">${c.works}</span>
      <span class="dim">${esc(shortDate(c.created_at))}</span><span class="dim">${esc(shortDate(c.used_at))}</span></div>`;
  }
  $("list").innerHTML = html;
  drawSidebarOptions();
}

// The right pane: each section's options.
function drawSidebarOptions() {
  const tick = (attrs, on, label, off = false) => `<label class="tickline"><input type="checkbox" ${attrs}${on ? " checked" : ""}${
    off ? " disabled" : ""}> ${label}</label>`;
  let html = `<h1>Sidebar</h1>
    <p class="where">Pins and hidden collections are saved with your collections, so they follow you to
    your other Macs. How each section looks is kept in this browser. A hidden collection keeps its papers
    and is still found by <code>#name</code>. Right-click a section's heading for the same options.</p>
    <div class="label">Sections</div>`;
  for (const { key, title } of state.sideSections ?? []) {
    const choices = SORT_CHOICES[key];
    html += `<div class="sbsec"><b>${esc(title)}</b>
      ${tick(`data-side-shown="${esc(key)}"`, !side.hidden.includes(key), "shown", KEEP_SECTIONS.has(key))}
      ${tick(`data-side-fold="${esc(key)}"`, side.folded[key], "folded")}
      ${choices ? `<label class="tickline">sort <select data-side-sort="${esc(key)}">${choices.map((m) =>
        `<option value="${m}"${side.sort[key] === m ? " selected" : ""}>${esc(SORTS[m])}</option>`).join("")}</select></label>` : ""}
      ${key === "project" || key === "course" ? tick(`data-side-empty="${key}"`, side.empty[key], "hide empty ones") : ""}
      ${key === "review" ? tick(`data-side-quiet="1"`, side.quiet, "only what needs a decision") : ""}</div>`;
  }
  html += `<div class="acts" style="margin-top:10px"><button class="act" data-side-reset="1">reset sections to the defaults</button></div>`;
  $("detail").innerHTML = html;
}

// The table: pin and hide boxes, the filter, the bulk buttons.
$("list").addEventListener("change", (event) => {
  const el = event.target;
  if (el.dataset?.sbPin) pinCollections([el.dataset.sbPin], el.checked);
  else if (el.dataset?.sbHide) hideCollections([el.dataset.sbHide], el.checked);
});
$("list").addEventListener("input", (event) => {
  if (!event.target.dataset?.sbFilter) return;
  state.sbFilter = event.target.value;
  drawSidebarPage();
  const box = $("list").querySelector(".sbfilter");
  box.focus(); box.setSelectionRange(box.value.length, box.value.length);
});
$("list").addEventListener("click", (event) => {
  const bulk = event.target.closest?.("[data-sb-bulk]")?.dataset.sbBulk;
  const name = event.target.closest?.("[data-coll-show]")?.dataset.collShow;
  if (name && state.view === "sidebar") { showCollection(name); return; }
  if (!bulk) return;
  const list = state.sidebar?.collections ?? [];
  if (bulk === "show") { hideCollections(list.filter((c) => c.hidden).map((c) => c.name), false); return; }
  const newest = list.filter((c) => c.kind === "course" && c.term).map((c) => c.term).sort((a, b) => termRank(b) - termRank(a))[0];
  const names = bulk === "empty" ? list.filter((c) => !c.hidden && !c.works).map((c) => c.name)
    : list.filter((c) => !c.name.includes("/") && c.kind === "course" && !c.hidden && c.term && c.term !== newest).map((c) => c.name);
  hideCollections(names);
});
// The sections' options.
$("detail").addEventListener("change", (event) => {
  const d = event.target.dataset ?? {};
  if (d.sideShown) hideSection(d.sideShown, !event.target.checked);
  else if (d.sideFold) foldSection(d.sideFold, event.target.checked);
  else if (d.sideSort) { side.sort[d.sideSort] = event.target.value; saveSide(); }
  else if (d.sideEmpty) { side.empty[d.sideEmpty] = event.target.checked; saveSide(); }
  else if (d.sideQuiet) { side.quiet = event.target.checked; saveSide(); }
});
$("detail").addEventListener("click", (event) => {
  if (!event.target.closest?.("[data-side-reset]")) return;
  side = structuredClone(SIDE_DEFAULT);
  saveSide();
  say("sidebar sections back to the defaults (pins and hidden collections stay)");
});

// --- context menus ----------------------------------------------------------------------
// Right-click anything that has actions of its own: a paper (or the selection),
// a course or project, a term, a sidebar section, a column header, a file, or
// a collection or tag in the detail pane. Each kind of thing has one builder
// (MENUS), which returns a title and its items; an item runs the same code as
// the button or key that does the same, and shows that key, so the menu also
// teaches the keys. "." (or ⇧F10) opens the highlighted paper's menu from the
// keyboard; ↑/↓ choose, Enter runs, Esc closes. In a text field, on selected
// text, or where nothing has a menu, the browser's own menu appears.

const SEP = { sep: true };
const ctx = { el: null, items: [], at: -1, anchor: null };

function closeMenu() {
  ctx.el?.remove();
  ctx.anchor?.classList.remove("ctxon");
  Object.assign(ctx, { el: null, items: [], at: -1, anchor: null });
}

// items: { label, key, run, off (true or the reason), danger } or SEP;
// false and null are skipped, so a builder can write `cond && {...}`.
function openMenu(x, y, title, items, anchor = null, keyboard = false) {
  closeMenu();
  const list = [];
  for (const item of items) {
    if (!item || (item.sep && (!list.length || list[list.length - 1].sep))) continue;
    list.push(item);
  }
  while (list.at(-1)?.sep) list.pop();
  if (!list.length) return false;
  const el = document.createElement("div");
  el.className = "ctx";
  el.setAttribute("role", "menu");
  el.innerHTML = (title ? `<div class="ctxtitle" title="${esc(title)}">${esc(title)}</div>` : "") +
    list.map((item, n) => item.sep ? `<hr>` :
      `<div class="ctxitem${item.danger ? " danger" : ""}${item.off ? " off" : ""}" role="menuitem" data-n="${n}"${
        typeof item.off === "string" ? ` title="${esc(item.off)}"` : ""}><span>${esc(item.label)}</span>${
        item.key ? `<kbd>${esc(item.key)}</kbd>` : ""}</div>`).join("");
  document.body.appendChild(el);
  // Keep it inside the window: open to the left of, or above, the pointer when there is no room.
  const box = el.getBoundingClientRect();
  el.style.left = `${Math.max(4, x + box.width > innerWidth - 4 ? x - box.width : x)}px`;
  el.style.top = `${Math.max(4, y + box.height > innerHeight - 4 ? innerHeight - 4 - box.height : y)}px`;
  anchor?.classList.add("ctxon");
  Object.assign(ctx, { el, items: list, at: -1, anchor });
  if (keyboard) moveMenu(1);
  return true;
}

function moveMenu(step) {
  const usable = ctx.items.map((item, n) => (item.sep || item.off ? -1 : n)).filter((n) => n >= 0);
  if (!usable.length) return;
  const at = usable.indexOf(ctx.at);
  ctx.at = usable[at < 0 ? (step > 0 ? 0 : usable.length - 1) : (at + step + usable.length) % usable.length];
  for (const row of ctx.el.querySelectorAll(".ctxitem")) row.classList.toggle("at", Number(row.dataset.n) === ctx.at);
}

// Run an item now, inside the click or key press (the clipboard needs that).
function runMenu(n) {
  const item = ctx.items[n];
  if (!item || item.sep || item.off) return;
  closeMenu();
  try {
    Promise.resolve(item.run()).catch((error) => say(String(error?.message ?? error), 8000));
  } catch (error) { say(String(error?.message ?? error), 8000); }
}

// A short name for a paper, for a menu's title.
function workTitle(item) {
  const who = item.authors?.length ? `${authorsShort(item.authors)} (${item.year ?? "n.d."}) ` : "";
  return who + (item.title ?? item.locations[0]?.path.split("/").pop() ?? "");
}

// A paper's menu: what its buttons and keys do. When rows are selected for a
// batch and this one is not among them, the menu is still about this paper
// (the selection stays); the pickers then open where the batch's do.
function rowMenu(row) {
  const i = Number(row.dataset.i), item = state.items[i];
  if (!item) return null;
  if (state.marked.has(item.id)) return batchMenu();
  const ready = state.selected === i ? detailDone : select(i, false);
  const w = item.work_id, has = item.locations.length > 0, marks = state.marked.size > 0;
  const path = item.locations[0]?.path;
  const here = state.collection && (item.own_collections ?? []).some((n) => n === state.collection
    || n.startsWith(state.collection + "/"));
  const after = (fn) => async () => { await ready; fn(); };
  return { title: workTitle(item), items: [
    has && { label: "open", key: "⏎", run: () => act("open") },
    has && { label: "open in Preview", key: "p", run: () => act("preview") },
    has && { label: "show in Finder", key: "f", run: () => act("reveal") },
    has && !item.status && { label: "identify it now", run: () => identify(path) },
    SEP,
    w && { label: item.queued ? "take off the queue" : "put on the queue", key: "q", run: () => act("queue") },
    w && { label: item.read_status === "read" ? "mark unread" : "mark read", key: "r", run: () => act("read") },
    w && item.read_status === "unread" && { label: "mark started", run: () => setRead(item, "reading") },
    SEP,
    item.citekey && { label: "copy citekey", key: "y", run: () => copyText(item.citekey, `copied ${item.citekey}`) },
    item.citekey && { label: "copy \\cite{…}", run: () => copyText(`\\cite{${item.citekey}}`, `copied \\cite{${item.citekey}}`) },
    w && { label: "copy BibTeX", key: "b", run: () => copyBibtex([w]) },
    SEP,
    w && { label: "add to collection…", key: "#",
           run: after(() => openCollectionPicker([w], marks ? $("batchpicker") : $("collpicker"))) },
    here && { label: `take out of #${state.collection}`, run: () => collect([w], state.collection, false) },
    has && { label: "file to…", key: "F", run: after(() => openPicker([path], "move", marks ? $("batchpicker") : null)) },
    has && { label: "copy to…", key: "C", run: after(() => openPicker([path], "copy", marks ? $("batchpicker") : null)) },
    SEP,
    w && !marks && { label: "write a note", key: "n", run: after(() => $("note")?.focus()) },
    w && !marks && { label: "edit the record", run: after(showEditor) },
    w && !marks && ["needs-review", "extracted", "imported"].includes(item.status)
      && { label: "accept the record as is", run: after(() => changeWork("confirm", {}, "accepted as is")) },
    SEP,
    { label: marks ? "add to the selection" : "select", key: "x", run: () => { markRow(i, true); paintMarks(); } },
  ] };
}

async function setRead(item, status) {
  const done = await post(`/api/work/${item.work_id}/read`, { status });
  if (done.detail) { say(done.detail); return; }
  Object.assign(item, done);
  say(`${item.citekey}: ${status === "reading" ? "started" : status}`);
  loadSidebar();
  if (["unread", "reading", "read"].includes(state.view)) { search(item.id); return; }
  const keep = state.selected;
  renderList(); select(keep, false);
}

// The selection's menu: the batch buttons, plus copying.
function batchMenu() {
  const items = [...state.marked.values()];
  const ids = items.map((i) => i.work_id).filter(Boolean);
  const keys = items.map((i) => i.citekey).filter(Boolean);
  const none = !items.some(primaryPath) && "none of them has a PDF yet";
  return { title: `${items.length} selected`, items: [
    { label: "file to…", key: "F", off: none, run: () => batchAct("move") },
    { label: "copy to…", key: "C", off: none, run: () => batchAct("copy") },
    { label: "add to collection…", key: "#", run: () => batchAct("collect") },
    state.collection && { label: `take out of #${state.collection}`, run: () => batchAct("uncollect") },
    SEP,
    { label: "queue all", run: () => batchAct("queue") },
    { label: "mark all read", run: () => batchAct("read") },
    SEP,
    keys.length && { label: "copy citekeys", run: () => copyText(keys.join(", "), `copied ${keys.length} citekeys`) },
    keys.length && { label: "copy \\cite{…}", run: () => copyText(`\\cite{${keys.join(", ")}}`, `copied \\cite with ${keys.length} keys`) },
    ids.length && { label: "copy BibTeX", run: () => copyBibtex(ids) },
    SEP,
    { label: "clear the selection", key: "esc", run: clearMarks },
  ] };
}

function showCollection(name) {
  state.collection = name; state.context = null; state.tag = null; state.view = "all";
  markSidebar(); search();
}

// A course's or project's menu (and a part's).
function collectionMenu(el) {
  const name = el.dataset.collection, c = state.collectionInfo?.[name];
  if (!c) return null;
  const top = !name.includes("/");
  // Moving: among the pins when opened on a pin, else among the projects.
  const group = el.dataset.drag ?? (top && c.kind !== "course" ? "project" : null);
  const shown = group ? groupOrder(group).filter((n) => group === "pinned" || state.showHidden.project
    || !tucked(state.collectionInfo[n] ?? {}, "project")) : [];
  const at = shown.indexOf(name);
  const folderOnly = c.implicit && "its papers are in it because of their folder: rename or move the folder instead";
  return { title: `#${name}`, items: [
    { label: "show its papers", run: () => showCollection(name) },
    { label: "new part…", run: () => renderNewCollection(state.collectionInfo[topName(name)]?.kind ?? c.kind, name + "/") },
    SEP,
    { label: `copy BibTeX (${c.works} ${c.works === 1 ? "paper" : "papers"})`, off: !c.works,
      run: () => copyText(get(`/api/collections/bibtex?name=${encodeURIComponent(name)}`).then((d) => d.bibtex ?? ""),
                          `copied BibTeX for #${name}'s ${c.works} papers`) },
    { label: `copy #${name}`, run: () => copyText(`#${name}`, `copied #${name}`) },
    SEP,
    c.folder && { label: "show its folder in Finder", run: () => openCollectionPlace(name, "folder") },
    c.bib_file && { label: "show its .bib in Finder", run: () => openCollectionPlace(name, "bib") },
    SEP,
    { label: c.pinned ? "unpin" : "pin to the top", run: () => pinCollections([name], !c.pinned) },
    group && { label: "move up", off: at <= 0, run: () => stepIn(group, name, "up") },
    group && { label: "move down", off: at < 0 || at >= shown.length - 1, run: () => stepIn(group, name, "down") },
    group && { label: "move to the top", off: at <= 0, run: () => stepIn(group, name, "top") },
    { label: c.hidden ? "show in the sidebar" : "hide from the sidebar", run: () => hideCollections([name], !c.hidden) },
    SEP,
    { label: "edit details…", run: () => renderNewCollection(c.kind, "", c) },
    { label: "rename…", off: folderOnly, run: () => renameCollection(c) },
    { label: "delete…", danger: true, off: folderOnly, run: () => deleteCollection(c) },
  ] };
}

async function openCollectionPlace(name, what) {
  const answer = await post("/api/collections/open", { name, what });
  if (answer.detail) say(answer.detail, 8000);
}

async function renameCollection(c) {
  const typed = prompt(`Rename #${c.name} (its parts follow) to:`, c.name);
  if (typed == null || !typed.trim() || typed.trim() === c.name) return;
  const answer = await post("/api/collections/rename", { old: c.name, new: typed.trim() });
  if (answer.detail) { say(answer.detail, 8000); return; }
  collectionList = null;
  const stays = c.from_folders ? "; papers in it because of their folder keep the old name" : "";
  say(`renamed #${answer.old} to #${answer.name} (${answer.papers} papers)${stays}`, 9000);
  if (state.collection && (state.collection === answer.old || state.collection.startsWith(answer.old + "/"))) {
    state.collection = answer.name + state.collection.slice(answer.old.length);
  }
  await loadSidebar(); markSidebar(); search();
}

async function deleteCollection(c) {
  const papers = c.works === 1 ? "1 paper" : `${c.works} papers`;
  if (!confirm(`Delete #${c.name}${c.name.includes("/") ? "" : " and its parts"}?\n\n` +
               `${c.works ? `Its ${papers} leave it but stay in the library` : "It has no papers"}` +
               `${c.from_folders ? " (papers in it because of their folder stay in it)" : ""}. ` +
               `Its details (term, folder, .bib) are forgotten; no file is touched.`)) return;
  const answer = await post("/api/collections/delete", { name: c.name });
  if (answer.detail) { say(answer.detail, 8000); return; }
  collectionList = null;
  say(`deleted #${answer.name} (${answer.papers} papers taken out)`, 8000);
  if (state.collection && (state.collection === answer.name || state.collection.startsWith(answer.name + "/"))) {
    state.collection = null; state.view = "all";
  }
  await loadSidebar(); markSidebar(); search();
}

// A term heading: hide or show all its courses at once.
function termMenu(el) {
  const term = el.dataset.term || null;
  const courses = (state.sidebar?.collections ?? [])
    .filter((c) => !c.name.includes("/") && c.kind === "course" && (c.term ?? null) === term);
  const shown = courses.filter((c) => !c.hidden).map((c) => c.name);
  const hidden = courses.filter((c) => c.hidden).map((c) => c.name);
  const n = (k) => `${k} ${k === 1 ? "course" : "courses"}`;
  return { title: term ?? "no term", items: [
    shown.length && { label: `hide its ${n(shown.length)}`, run: () => hideCollections(shown) },
    hidden.length && { label: `show its ${n(hidden.length)} hidden`, run: () => hideCollections(hidden, false) },
  ] };
}

// A sidebar section's options (its heading; for Projects and Courses also
// their "N hidden" line and "+ new"): sort, fold, hide, and what it lists.
function sectionMenu(el) {
  const key = el.dataset.section ?? el.dataset.showHidden ?? el.dataset.newcoll;
  const kind = key === "project" || key === "course" ? key : null;
  const pick = (on, label) => `${on ? "✓" : " "} ${label}`;
  const tops = kind ? sectionTops(kind) : [];
  const away = tops.filter((c) => tucked(c, kind)).length;
  const hidden = tops.filter((c) => c.hidden).map((c) => c.name);
  const pinned = key === "pinned" ? Object.values(state.collectionInfo).filter((c) => c.pinned).map((c) => c.name) : [];
  return { title: sectionTitle(key), items: [
    kind && { label: `new ${kind}…`, run: () => renderNewCollection(kind) },
    SEP,
    // Sort: a choice of orders, the current one ticked.
    ...(SORT_CHOICES[key] ?? []).map((mode) => ({ label: pick(side.sort[key] === mode, `sort ${SORTS[mode]}`),
      run: () => { side.sort[key] = mode; saveSide(); } })),
    SEP,
    kind && { label: pick(side.empty[kind], "hide empty ones"), run: () => {
      side.empty[kind] = !side.empty[kind]; saveSide(); } },
    key === "review" && { label: pick(side.quiet, "only what needs a decision"), run: () => {
      side.quiet = !side.quiet; saveSide(); } },
    kind && away && { label: state.showHidden[kind] ? `fold the ${away} hidden` : `show the ${away} hidden`,
      run: () => { state.showHidden[kind] = !state.showHidden[kind]; drawSidebar(); } },
    hidden.length && { label: `bring all ${hidden.length} hidden back`, run: () => hideCollections(hidden, false) },
    pinned.length && { label: `unpin all ${pinned.length}`, run: () => pinCollections(pinned, false) },
    SEP,
    { label: side.folded[key] ? "unfold" : "fold", run: () => foldSection(key) },
    !KEEP_SECTIONS.has(key) && { label: "hide this section", run: () => hideSection(key) },
    side.hidden.length && { label: `show the ${side.hidden.length} hidden ${side.hidden.length === 1 ? "section" : "sections"}`,
      run: () => { side.hidden = []; saveSide(); } },
    { label: "manage the sidebar…", run: showSidebarView },
  ] };
}

function showSidebarView() {
  state.collection = null; state.context = null; state.tag = null; state.view = "sidebar";
  markSidebar(); search();
}

// A column header: sort, hide, sizes.
function columnMenu(el) {
  const id = el.dataset.col, column = COLUMNS[id];
  const sorted = layout.sort?.col === id ? layout.sort.dir : null;
  return { title: column.name ?? column.label, items: [
    { label: "sort ascending", off: sorted === "asc", run: () => setSort({ col: id, dir: "asc" }) },
    { label: "sort descending", off: sorted === "desc", run: () => setSort({ col: id, dir: "desc" }) },
    layout.sort && { label: "default order", run: () => setSort(null) },
    SEP,
    { label: "hide this column", off: visibleColumns().length === 1, run: () => {
      layout.hidden = [...layout.hidden, id];
      if (layout.sort?.col === id) layout.sort = null;
      saveLayout(); applyLayout(); sortItems(); renderList(); } },
    { label: "columns…", run: () => { renderColumnMenu(); $("colmenu").hidden = false; } },
    layout.widths[id] != null && { label: "reset its width", run: () => {
      delete layout.widths[id]; saveLayout(); applyLayout(); } },
    { label: "reset all columns", run: () => {
      Object.assign(layout, { order: [...DEFAULT_LAYOUT.order], hidden: [], widths: {}, sort: null });
      saveLayout(); applyLayout(); sortItems(); renderList(); } },
  ] };
}

// One PDF of the paper in the detail pane.
function fileMenu(el) {
  const path = el.dataset.path, item = state.items[state.selected];
  const several = (item?.locations.length ?? 0) > 1;
  const name = path.split("/").pop();
  return { title: name, items: [
    { label: "open", run: () => window.open(`/file?path=${encodeURIComponent(path)}`, "_blank") },
    { label: "open in Preview", run: () => post("/api/open", { path }) },
    { label: "show in Finder", run: () => post("/api/open", { path, reveal: true }) },
    SEP,
    { label: "file to…", run: () => openPicker([path], "move") },
    { label: "copy to…", run: () => openPicker([path], "copy") },
    el.querySelector("[data-pull]") && { label: "bring its marks into the library copy", run: () => pullMarks(path) },
    SEP,
    { label: "copy the file name", run: () => copyText(name, `copied ${name}`) },
    SEP,
    { label: "move this copy to the Trash…", danger: true,
      off: !several && "the only copy: refs never trashes the last one", run: () => trashCopy(path) },
  ] };
}

// A collection chip in the detail pane.
function chipMenu(el) {
  const name = el.dataset.collShow, own = el.closest(".chip")?.querySelector("[data-coll-out]");
  const w = state.items[state.selected]?.work_id;
  return { title: `#${name}`, items: [
    { label: "show its papers", run: () => showCollection(name) },
    w && { label: "take this paper out", off: !own && "it is in it because of its folder", run: () => collect([w], name, false) },
  ] };
}

// A tag in the detail pane (or a field in the sidebar).
function tagMenu(el) {
  const tag = el.dataset.tag, term = `tag:${tag.replace(/\s+/g, "-")}`;
  return { title: tag, items: [
    { label: "show papers with this tag", run: () => {
      state.tag = tag; state.context = null; state.collection = null; state.view = "all"; markSidebar(); search(); } },
    { label: "add to the search", run: () => {
      state.query = [state.query, term].filter(Boolean).join(" "); setAdding(false); search(); } },
  ] };
}

// Most specific first: a file inside the detail pane before the pane, etc.
const MENUS = [
  ["#detail .loc[data-path]", fileMenu],
  ["#detail .chip .tag[data-coll-show]", chipMenu],
  ["#detail .tag[data-tag], #side .nav[data-tag]", tagMenu],
  ["#list .hc[data-col]", columnMenu],
  ["#list .row[data-i]", rowMenu],
  ["#side .nav[data-collection]", collectionMenu],
  ["#side .group[data-term]", termMenu],
  ["#side [data-section], #side [data-show-hidden], #side [data-newcoll]", sectionMenu],
];

document.addEventListener("contextmenu", (event) => {
  closeMenu();
  const target = event.target;
  if (!(target instanceof Element) || $("onboard") || $("help").open) return;
  if (target.closest("input, textarea, select, [contenteditable]")) return;       // the browser's: paste, spelling
  const selection = getSelection();
  if (selection && !selection.isCollapsed && selection.containsNode(target, true)) return;   // copy what you selected
  for (const [selector, build] of MENUS) {
    const el = target.closest(selector);
    if (!el) continue;
    const menu = build(el);
    const anchor = el.closest(".nav, .row, .hc, .loc, .group, .label, .tag") ?? el;
    if (menu && openMenu(event.clientX, event.clientY, menu.title, menu.items, anchor)) event.preventDefault();
    return;
  }
});

// The highlighted paper's menu from the keyboard, under its row.
function rowMenuFromKeys() {
  const row = $("list").querySelector(".row.sel");
  if (!row) return;
  const menu = rowMenu(row), box = row.getBoundingClientRect();
  if (menu) openMenu(box.left + 6 * charWidth, box.bottom, menu.title, menu.items, row, true);
}

// While a menu is open it has the keyboard: ↑/↓ (or j/k) choose, Enter runs,
// Esc closes; any other key closes it and does what it always does.
window.addEventListener("keydown", (event) => {
  if (!ctx.el) return;
  const moves = { ArrowDown: 1, j: 1, ArrowUp: -1, k: -1 };
  if (event.key in moves) moveMenu(moves[event.key]);
  else if (event.key === "Enter" || event.key === " ") runMenu(ctx.at);
  else if (event.key === "Escape") closeMenu();
  else { closeMenu(); return; }
  event.preventDefault();
  event.stopPropagation();
}, true);
window.addEventListener("mousedown", (event) => { if (ctx.el && !ctx.el.contains(event.target)) closeMenu(); }, true);
window.addEventListener("wheel", (event) => { if (ctx.el && !ctx.el.contains(event.target)) closeMenu(); }, { capture: true, passive: true });
window.addEventListener("blur", closeMenu);
window.addEventListener("resize", closeMenu);
document.addEventListener("mousemove", (event) => {
  const row = ctx.el && event.target.closest?.(".ctxitem:not(.off)");
  if (row && Number(row.dataset.n) !== ctx.at) {
    ctx.at = Number(row.dataset.n);
    for (const r of ctx.el.querySelectorAll(".ctxitem")) r.classList.toggle("at", r === row);
  }
});
document.addEventListener("click", (event) => {
  const row = ctx.el && event.target.closest?.(".ctxitem");
  if (!row) return;
  event.stopPropagation();          // the page's own click handlers (which close the columns menu) stay out
  runMenu(Number(row.dataset.n));
}, true);

// --- keyboard ---------------------------------------------------------------------------

const HELP = `keys

  j / k  or  ↓ / ↑     move
  g / G                top / bottom
  /                    search          esc   clear
  .  or  ⇧F10          this paper's menu (or the selection's): everything you can do with it
  right-click          a menu for whatever is under the pointer: a paper, a course or project,
                       a term, a column header, a file, a tag
  enter  or  o         open the pdf in a new tab
  p                    open in Preview
  f                    show in Finder
  F                    file to… (move the PDF into a course or project folder)
  C                    copy to… (same, but the original stays where it is)
                       in the box: ↑/↓ choose, Tab then a name = new folder
  u                    undo the last file to… / copy to…
  #                    add to a collection (a course or project; a new name makes one)
                       × on a collection takes the paper out (dotted ones come from its folder)

selecting many

  x                    select / unselect this row (and move down)
  ⌘-click  ⇧-click     add one row / a range
  ⌘A                   select every row in the list
  esc                  clear the selection
  F  C  #              file / copy / collect the whole selection (buttons: queue all, mark read)
  y                    copy citekey
  b                    copy BibTeX
  r                    toggle read / unread
  q                    put on the queue / take off (▸ in the list)
  n                    write a note (saved to Obsidian; esc leaves the box)
  a                    add a paper (URL, DOI, arXiv id, NBER number); shift+Enter also queues it
  m                    show course material too
  R                    rescan Dropbox
  w                    the folder watcher: what it is reading now, and what it did
  ,                    settings (folders, notes, AI, API key)
  t                    light / dark

sidebar

  drag a project       move it among the projects (a pin among the pins); the section
                       switches to "my order"
  click a heading      fold or unfold the section
  right-click          a heading: sort, hide empty ones, fold, hide the section;
                       a course or project: pin, hide, move, rename, edit, delete;
                       a term: hide its courses. "N hidden" shows the hidden ones
  sidebar (under refs) every collection, to pin or hide many at once; every section's options

review (sidebar)

  renames              tick files, then "rename selected"; undo is offered after
  copies               the same PDF in several places: trash a copy or keep all

bookmarklet            open /bookmarklet and drag "+ refs" to the bookmarks bar

search filters

  #220a   #219a/public-economics   (a collection; a parent includes its parts)
  author:smith   year:2019   course:101   project:thesis
  tag:theory    kind:book   status:needs-review   topic:demand
  read:read     read:reading   read:unread   queued:yes

reading

  your highlights and notes in a PDF count: marks on 30% of its pages (at
  least 5) make it read (●), marks on 2 or more pages make it started (◐).
  What you set with r always wins. ▸ = on the queue.`;

document.addEventListener("keydown", async (event) => {
  if ($("help").open) { $("help").close(); return; }
  if ($("onboard")) return;                  // first-run screens: no shortcuts
  if (event.target.id === "note") {
    if (event.key === "Escape") event.target.blur();          // blurring saves
    else if ((event.metaKey || event.ctrlKey) && (event.key === "s" || event.key === "Enter")) {
      event.preventDefault(); saveNote();
    }
    return;
  }
  if (event.target.matches?.("input, textarea, select")) {
    if (event.key === "Escape" && event.target !== $("q")) {
      $("editor") && ($("editor").innerHTML = "");
      event.target.closest(".picker")?.replaceChildren();
    }
    return;                                   // typing in a field: no shortcuts
  }
  if ((event.metaKey || event.ctrlKey) && event.key === "a" && state.items.length) {
    event.preventDefault();                   // select every row, not the page's text
    state.items.forEach((_, i) => markRow(i, true));
    paintMarks();
    return;
  }
  if (event.metaKey || event.ctrlKey || event.altKey) return;
  const key = event.key;
  const moves = { j: 1, ArrowDown: 1, k: -1, ArrowUp: -1 };
  if (key in moves) { event.preventDefault(); select(state.selected + moves[key]); }
  else if (key === "g") select(0);
  else if (key === "G") select(state.items.length - 1);
  else if (key === "/") { event.preventDefault(); setAdding(false); $("q").focus(); $("q").select(); }
  else if (key === "a") { event.preventDefault(); setAdding(true); }
  else if (key === "Enter" || key === "o") act("open");
  else if (key === "." || (key === "F10" && event.shiftKey)) { event.preventDefault(); rowMenuFromKeys(); }
  else if (key === "p") act("preview");
  else if (key === "f") act("reveal");
  else if (key === "F" || key === "C") {
    event.preventDefault();
    if (state.marked.size) { batchAct(key === "C" ? "copy" : "move"); return; }
    const p = state.items[state.selected]?.locations[0]?.path;
    if (p) openPicker([p], key === "C" ? "copy" : "move");
  }
  else if (key === "#") {
    event.preventDefault();
    if (state.marked.size) { batchAct("collect"); return; }
    const id = state.items[state.selected]?.work_id;
    if (id) openCollectionPicker([id], $("collpicker"));
  }
  else if (key === "x" && state.items.length) {
    markRow(state.selected);
    select(state.selected + 1);              // also redraws the detail pane
    paintMarks();
  }
  else if (key === "u" && state.lastFiled) undoBatch(state.lastFiled);
  else if (key === "Escape" && state.marked.size) clearMarks();
  else if (key === "y") act("citekey");
  else if (key === "b") act("bibtex");
  else if (key === "r") act("read");
  else if (key === "q") act("queue");
  else if (key === "n" && $("note")) { event.preventDefault(); $("note").focus(); }
  else if (key === "m") {
    state.scope = state.scope === "all" ? "library" : "all";
    say(state.scope === "all" ? "showing course material too" : "papers only");
    search();
  }
  else if (key === "R") {
    say("scanning …", 0);
    const result = await post("/api/scan");
    say(`scanned ${result.seen} pdfs, ${result.changes} changes`);
    loadSidebar(); search();
  }
  else if (key === "t") {
    const dark = getComputedStyle(document.documentElement).getPropertyValue("--bg").trim() === "#101211";
    document.documentElement.dataset.theme = dark ? "light" : "dark";
    try { localStorage.setItem("refs-theme", document.documentElement.dataset.theme); } catch {}
  }
  else if (key === "?") { $("helptext").textContent = HELP; $("help").showModal(); }
  else if (key === "w") toggleWatchPanel();
  else if (key === "Escape" && !$("watchpanel").hidden) toggleWatchPanel(false);
  else if (key === ",") {
    state.context = null; state.tag = null; state.collection = null; state.view = "settings";
    markSidebar(); search();
  }
});

// --- first run: onboarding -------------------------------------------------------------------

// Shown while refs is not set up (no watched folders). One screen per question,
// over the main panes; the answers are saved together at the end through
// /api/settings, with the same checks as the settings page (settings.py), and
// the watcher then scans (and reads, if you chose so) with progress shown here.
const ob = {
  step: 0, info: null, library: null, survey: null, open: new Set(),
  roots: new Set(), dump: "papers-dump", exclude: new Set(), noLlm: new Set(),
  vault: "", vaultFolder: "refs", read: "dump", saving: false, startedPass: null,
};
const OB_STEPS = ["welcome", "library", "papers", "dump", "private", "notes", "reading", "progress"];

async function maybeOnboard() {
  const info = await get("/api/onboarding");
  if (!info.needed) return;
  ob.info = info;
  ob.library = info.libraries[0]?.path ?? info.library;
  ob.dump = info.dump;
  ob.vault = info.vaults[0] ?? "";
  let box = $("onboard");
  if (!box) {
    box = document.createElement("section");
    box.id = "onboard";
    box.setAttribute("aria-label", "Set up refs");
    document.querySelector("main").append(box);
  }
  drawOnboarding();
}

const obUnder = (path, folders) => [...folders].some((f) => path === f || path.startsWith(f + "/"));
// PDFs in the chosen folders (a folder inside another chosen one counts once).
function obPdfs() {
  let n = 0;
  const walk = (node) => {
    if (ob.roots.has(node.path) && !obUnder(node.path, ob.exclude)) { n += node.pdfs; return; }
    node.children.forEach(walk);
  };
  if (ob.survey) walk(ob.survey.tree);
  return n;
}
// Folders of term folders inside the chosen ones: courses grouped by term.
const obRules = () => (ob.survey?.term_rules ?? []).filter((r) => obUnder(r.split("/{term}")[0], ob.roots));
const dollars = (x) => (x < 0.01 ? "under $0.01" : `about $${x < 10 ? x.toFixed(2) : Math.round(x)}`);

function obTree(node, depth) {
  // Folders without PDFs are left out, except at the top (dimmed).
  const kids = node.children.filter((c) => c.pdfs > 0 || depth === 0);
  return kids.map((c) => {
    const inside = obUnder(c.path, [...ob.roots].filter((r) => r !== c.path));
    const checked = ob.roots.has(c.path) || inside;
    const more = c.children.some((g) => g.pdfs > 0);
    const opened = ob.open.has(c.path);
    return `<div class="ob-row${c.pdfs ? "" : " dim"}" style="padding-left:${depth * 2.5}ch">
      <button class="act ob-fold" data-ob-fold="${esc(c.path)}"${more ? "" : " disabled"}>${more ? (opened ? "▾" : "▸") : " "}</button>
      <label><input type="checkbox" data-ob-root="${esc(c.path)}"${checked ? " checked" : ""}${inside ? " disabled" : ""}>
      ${esc(c.name)}</label><span class="n">${c.pdfs.toLocaleString()}</span></div>`
      + (opened ? obTree(c, depth + 1) : "");
  }).join("");
}

function drawOnboarding() {
  const box = $("onboard");
  const info = ob.info;
  const name = OB_STEPS[ob.step];
  const back = ob.step > 1 && name !== "progress" ? `<button class="act" data-ob-back="1">back</button>` : "";
  const next = (label = "next", disabled = false) =>
    `<button class="act primary" data-ob-next="1"${disabled ? " disabled" : ""}>${label}</button>`;
  const dots = OB_STEPS.slice(1, -1).map((_, i) => `<span class="${i + 1 <= ob.step ? "on" : ""}">·</span>`).join("");
  let body = "";

  if (name === "welcome") {
    body = `<h1>Set up refs</h1>
      <p>refs indexes the PDFs you already keep in folders (Dropbox, iCloud Drive, any folder),
      works out what each paper is, and follows the folders as you add, move and rename files.
      Your files stay where they are.</p>
      <p class="where">A few questions: where your papers are, what to keep private, and how
      much to read with Claude. Everything can be changed later under settings.</p>
      <div class="ob-nav">${next("start")}</div>`;
  }
  else if (name === "library") {
    const options = [...info.libraries];
    if (!options.some((o) => o.path === ob.library)) options.push({ label: "chosen", path: ob.library });
    body = `<h1>Where do you keep your papers?</h1>
      <p class="where">The library folder: refs looks for papers inside it, and nowhere else.
      Usually your Dropbox or iCloud Drive.</p>
      <div class="ob-choices">${options.map((o) => `<label><input type="radio" name="ob-lib" value="${esc(o.path)}"
        ${o.path === ob.library ? " checked" : ""}> ${esc(o.label)} <span class="badge">${esc(o.path)}</span></label>`).join("")}</div>
      <button class="act" data-ob-choose="library">choose another folder…</button>
      <div class="ob-nav">${back}${next()}</div>`;
  }
  else if (name === "papers") {
    if (!ob.survey) body = `<h1>Counting PDFs …</h1><p class="where">in ${esc(ob.library)}</p>`;
    else if (ob.survey.error) body = `<h1>That folder cannot be read</h1><p class="bad">${esc(ob.survey.error)}</p>
      <div class="ob-nav">${back}</div>`;
    else {
      const n = obPdfs();
      body = `<h1>Which folders hold papers?</h1>
        <p class="where">Tick the folders refs should watch; everything inside them is included.
        Numbers are PDFs. ▸ opens a folder, so you can pick only part of it.</p>
        ${ob.survey.complete ? "" : `<p class="bad">This library is large: counting stopped after
          ${ob.survey.seconds} s, so the numbers are low.</p>`}
        <div class="ob-tree">${obTree(ob.survey.tree, 0)}</div>
        <p class="ob-sum">${ob.roots.size ? `${n.toLocaleString()} PDFs in ${ob.roots.size} folder${ob.roots.size > 1 ? "s" : ""}` : "nothing chosen yet"}</p>
        ${obRules().map((r) => `<p class="where">Term folders in <b>${esc(r.split("/{term}")[0])}</b>:
          the folders inside each term are courses, grouped by term.</p>`).join("")}
        <div class="ob-nav">${back}${next("next", !ob.roots.size)}</div>`;
    }
  }
  else if (name === "dump") {
    body = `<h1>Where should new papers go?</h1>
      <p class="where">The quick-add folder. Papers you add in refs (a link, a DOI, the
      bookmarklet) are saved here, and you can save PDFs here yourself from the browser or the
      phone. refs gives them tidy names; it renames nothing anywhere else. Created if it does
      not exist, inside the library folder.</p>
      <div class="adder"><input id="ob-dump" value="${esc(ob.dump)}" spellcheck="false" autocomplete="off"></div>
      <div class="ob-nav">${back}${next()}</div>`;
  }
  else if (name === "private") {
    const shown = (ob.survey?.suggestions ?? []).filter((s) => obUnder(s.path, ob.roots));
    const row = (path, kind) => {
      const set = kind === "no_llm" ? ob.noLlm : ob.exclude;
      return `<label><input type="checkbox" data-ob-private="${esc(path)}" data-kind="${kind}"${set.has(path) ? " checked" : ""}>
        ${esc(path)} <span class="badge">· ${kind === "no_llm" ? "index, but never send to Claude" : "leave out entirely"}</span></label>`;
    };
    const chosen = [...ob.exclude].map((p) => [p, "exclude"]).concat([...ob.noLlm].map((p) => [p, "no_llm"]));
    const listed = new Set(shown.map((s) => s.path));
    body = `<h1>Anything private in there?</h1>
      <p class="where">Paperwork (visa, taxes, insurance) can be <b>left out entirely</b>: never
      read, never indexed. Confidential work (a manuscript you referee) can be indexed but
      <b>never sent to Claude</b>.</p>
      ${shown.length ? `<p>Suggested from folder names:</p>` : `<p class="where">No folder names suggest paperwork.</p>`}
      <div class="ob-choices">${shown.map((s) => row(s.path, s.kind)).join("")}
        ${chosen.filter(([p]) => !listed.has(p)).map(([p, k]) => row(p, k)).join("")}</div>
      <div class="acts"><button class="act" data-ob-choose="exclude">leave out a folder…</button>
        <button class="act" data-ob-choose="no_llm">keep a folder from Claude…</button></div>
      <div class="ob-nav">${back}${next()}</div>`;
  }
  else if (name === "notes") {
    const vaults = [...new Set([...info.vaults, ob.vault].filter(Boolean))];
    body = `<h1>Reading notes in Obsidian?</h1>
      <p class="where">refs can keep a note for each paper as a Markdown file in an Obsidian
      vault, so your notes are in Obsidian too and <code>[[citekey]]</code> links to them.</p>
      <div class="ob-choices">${vaults.map((v) => `<label><input type="radio" name="ob-vault" value="${esc(v)}"
        ${v === ob.vault ? " checked" : ""}> ${esc(v)}</label>`).join("")}
        <label><input type="radio" name="ob-vault" value=""${ob.vault ? "" : " checked"}> no notes for now</label></div>
      <button class="act" data-ob-choose="vault">choose a vault…</button>
      ${ob.vault ? `<p style="margin-top:10px">Folder in the vault:</p>
        <div class="adder"><input id="ob-vault-folder" value="${esc(ob.vaultFolder)}" spellcheck="false"></div>` : ""}
      <div class="ob-nav">${back}${next()}</div>`;
  }
  else if (name === "reading") {
    const n = obPdfs();
    const cost = n * info.cost_per_pdf;
    const minutes = Math.max(1, Math.round(n * 4 / 60));
    const key = { set: "found in the macOS Keychain", environment: "from ANTHROPIC_API_KEY" }[info.api_key];
    const option = (value, label, note) => `<label><input type="radio" name="ob-read" value="${value}"
      ${ob.read === value ? " checked" : ""}> ${label} <span class="badge">· ${note}</span></label>`;
    body = `<h1>Reading your papers</h1>
      <p>To tell what each PDF is, refs sends the <b>text of its first three pages</b> (an image
      of page one for scans) to Claude Haiku, then checks the result against Crossref and
      OpenAlex. It never sends file names, your notes, or anything in a private folder.
      It costs about $0.001 a PDF, billed to your own Anthropic API key.</p>
      <div class="label">API key</div>
      ${key ? `<p class="where">${esc(key)} ✓</p>` : `<p class="where">Create one at
        <a href="https://console.anthropic.com/settings/keys" target="_blank" rel="noreferrer">console.anthropic.com</a>,
        then paste it here. It is kept in the macOS Keychain, never in a file.</p>
        <div class="adder"><input id="ob-key" type="password" placeholder="sk-ant-…" autocomplete="off" spellcheck="false">
        <button class="act" data-ob-key="1">check and save</button></div>`}
      <div class="label">What to read</div>
      <div class="ob-choices">
        ${option("all", `everything: ${n.toLocaleString()} PDFs now`, `${dollars(cost)}, ${minutes} min or so, then each new PDF`)}
        ${option("dump", "only papers I add from now on", "the others wait under “not identified”, free")}
        ${option("none", "nothing for now", "read later with `refs process`")}
      </div>
      ${ob.read === "all" && cost > info.read_cap ? `<p class="where">That is above the monthly limit
        for reading ($${info.read_cap}): reading stops there and goes on next month, or raise
        the limit under settings.</p>` : ""}
      ${info.api_key === "missing" && ob.read !== "none" ? `<p class="bad">Without a key nothing can be read;
        PDFs are still indexed and found by file name.</p>` : ""}
      <div class="ob-nav">${back}${next(ob.saving ? "saving …" : "finish setup", ob.saving)}</div>
      ${ob.error ? `<p class="bad">${esc(ob.error)}</p>` : ""}`;
  }
  else if (name === "progress") body = obProgressHtml();

  box.innerHTML = `<div class="ob-page"><div class="ob-dots">${dots}</div>${body}</div>`;
}

let obStatus = null;
function obProgressHtml() {
  const s = obStatus;
  const p = s?.progress;
  const finished = s && !p && !s.pending && s.last_pass && s.last_pass !== ob.startedPass;
  if (!ob.info.watching) {
    return `<h1>Saved</h1><p class="where">This refs runs without the folder watcher (--no-watch),
      so it indexed your folders but reads nothing new. Run <code>refs process --all</code> to read.</p>
      <div class="ob-nav"><button class="act primary" data-ob-done="1">open my library</button></div>`;
  }
  if (!finished) {
    const what = !p ? "starting …"
      : p.stage === "scanning" ? `scanning: ${p.done.toLocaleString()} of ${p.total.toLocaleString()} PDFs`
      : p.stage === "indexing" ? "building the search index (the first time, this downloads a 70 MB search model)"
      : p.stage === "filing" ? "filing what was read and updating the index"
      : `reading with Claude: ${p.done.toLocaleString()} of ${p.total.toLocaleString()} PDFs`;
    const share = p && p.total ? Math.round((100 * p.done) / p.total) : 0;
    return `<h1>Setting up your library</h1><p>${esc(what)}</p>
      <div class="ob-bar"><span style="width:${share}%"></span></div>
      <p class="where">You can use refs while this runs; reading continues in the background.</p>
      <div class="ob-nav"><button class="act" data-ob-done="1">use refs now</button></div>`;
  }
  const login = { running: "refs runs in the background and starts at login ✓",
                  installed: "refs is set to start at login",
                  none: "To keep refs running in the background and start it at login, run <code>refs service install</code> in Terminal." }[ob.info.service];
  return `<h1>Your library is ready</h1>
    <p>${esc(s.message || "Indexed.")}</p>
    <div class="label">Next</div>
    <ul class="ob-list">
      <li>Search with <kbd>/</kbd>, open with <kbd>⏎</kbd>, all keys with <kbd>?</kbd>.</li>
      <li>Add papers from any page with the <a href="/bookmarklet" target="_blank">bookmarklet</a>, or with <kbd>a</kbd> here.</li>
      ${ob.read !== "all" ? `<li>Your PDFs are indexed but not identified yet: they are under
        <b>not identified</b> in the sidebar, found by file name. To identify them, set
        <b>Read new PDFs</b> to “all” under settings (about $0.001 each).</li>` : ""}
      <li>${login}</li>
      <li>Change any of this under <b>settings</b> (bottom of the sidebar, or <kbd>,</kbd>), including
      how folders become courses and projects.</li>
    </ul>
    <div class="ob-nav"><button class="act primary" data-ob-done="1">open my library</button></div>`;
}

async function obFollow() {
  // Poll the watcher until the pass that the new settings started is over.
  while (OB_STEPS[ob.step] === "progress" && $("onboard")) {
    try { obStatus = await get("/api/changes"); } catch {}
    drawOnboarding();
    const finished = obStatus && !obStatus.progress && !obStatus.pending
      && obStatus.last_pass && obStatus.last_pass !== ob.startedPass;
    if (finished || !ob.info.watching) return;
    await new Promise((r) => setTimeout(r, 1000));
  }
}

async function obNext() {
  const name = OB_STEPS[ob.step];
  const box = $("onboard");
  if (name === "library") {
    ob.library = box.querySelector("input[name=ob-lib]:checked")?.value ?? ob.library;
    ob.survey = null; ob.roots.clear(); ob.open.clear();
    ob.exclude.clear(); ob.noLlm.clear(); ob.suggested = false;
    ob.step++; drawOnboarding();
    const answer = await fetch(`/api/onboarding/survey?root=${encodeURIComponent(ob.library)}`);
    ob.survey = await answer.json();
    return drawOnboarding();
  }
  if (name === "dump") {
    ob.dump = $("ob-dump").value.trim().replace(/^\/+|\/+$/g, "") || ob.info.dump;
    // Suggestions marked likely (visa, referee, …) start ticked, once.
    if (!ob.suggested) {
      ob.suggested = true;
      for (const s of ob.survey?.suggestions ?? []) {
        if (s.ticked && obUnder(s.path, ob.roots)) (s.kind === "no_llm" ? ob.noLlm : ob.exclude).add(s.path);
      }
    }
  }
  if (name === "notes") {
    ob.vault = box.querySelector("input[name=ob-vault]:checked")?.value ?? "";
    ob.vaultFolder = $("ob-vault-folder")?.value.trim() || "refs";
  }
  if (name === "reading") return obFinish();
  ob.step++;
  // First time on the reading screen: read everything when that is cheap (up
  // to $5) and a key is there; otherwise only papers added from now on.
  if (OB_STEPS[ob.step] === "reading" && !ob.readChosen) {
    ob.read = obPdfs() * ob.info.cost_per_pdf <= 5 && ob.info.api_key !== "missing" ? "all" : "dump";
  }
  drawOnboarding();
}

async function obFinish() {
  ob.read = $("onboard").querySelector("input[name=ob-read]:checked")?.value ?? ob.read;
  ob.saving = true; ob.error = null; drawOnboarding();
  ob.startedPass = (await get("/api/changes")).last_pass;
  const changes = {
    dropbox_root: ob.library, roots: [...ob.roots], dump: ob.dump,
    exclude_paths: [...ob.exclude], no_llm: [...ob.noLlm],
    obsidian_vault: ob.vault, obsidian_folder: ob.vaultFolder, auto_process: ob.read,
    contexts: obRules(),
  };
  // "Read everything" showed its cost: it is your OK for that batch (budget.py).
  const response = await fetch("/api/settings", { method: "POST",
    headers: { "Content-Type": "application/json", "X-Refs": "1" },
    body: JSON.stringify({ changes, approve_reads: ob.read === "all" }) });
  const answer = await response.json();
  ob.saving = false;
  if (!response.ok) { ob.error = answer.error; return drawOnboarding(); }
  ob.step++;
  obFollow();
}

document.addEventListener("click", async (event) => {
  const box = $("onboard");
  const el = event.target.closest("button");
  if (!box || !el || !box.contains(el)) return;
  const d = el.dataset;
  if (d.obNext) await obNext();
  else if (d.obBack) { ob.step--; drawOnboarding(); }
  else if (d.obFold) { ob.open.has(d.obFold) ? ob.open.delete(d.obFold) : ob.open.add(d.obFold); drawOnboarding(); }
  else if (d.obChoose) {
    const kind = d.obChoose;
    const answer = await post("/api/settings/folder", {
      prompt: { library: "refs: the library folder", vault: "refs: your Obsidian vault",
                exclude: "refs: a folder to leave out", no_llm: "refs: a folder to keep from Claude" }[kind],
      kind: kind === "library" || kind === "vault" ? kind : "folders" });
    if (answer.error) say(answer.error, 8000);
    if (!answer.path) return;
    if (kind === "library") ob.library = answer.path;
    else if (kind === "vault") ob.vault = answer.path;
    else (kind === "no_llm" ? ob.noLlm : ob.exclude).add(answer.path);
    drawOnboarding();
  }
  else if (d.obKey) {
    const input = $("ob-key");
    say("checking the key with Anthropic …", 0);
    const response = await fetch("/api/settings/key", { method: "POST",
      headers: { "Content-Type": "application/json", "X-Refs": "1" }, body: JSON.stringify({ key: input.value }) });
    const answer = await response.json();
    if (!response.ok) { say(answer.error, 8000); return; }
    say("key saved in the Keychain");
    ob.info.api_key = answer.api_key;
    // With a key now, the default may become "read everything" (if cheap).
    if (!ob.readChosen) ob.read = obPdfs() * ob.info.cost_per_pdf <= 5 ? "all" : "dump";
    drawOnboarding();
  }
  else if (d.obDone) {
    box.remove();
    loadSidebar(); search();
  }
});

document.addEventListener("change", (event) => {
  const box = $("onboard");
  const el = event.target;
  if (!box || !box.contains(el)) return;
  if (el.dataset.obRoot !== undefined) {
    const path = el.dataset.obRoot;
    if (el.checked) {
      // A folder replaces any chosen folders inside it.
      for (const r of [...ob.roots]) if (r.startsWith(path + "/")) ob.roots.delete(r);
      ob.roots.add(path);
    } else ob.roots.delete(path);
    drawOnboarding();
  } else if (el.dataset.obPrivate !== undefined) {
    const set = el.dataset.kind === "no_llm" ? ob.noLlm : ob.exclude;
    el.checked ? set.add(el.dataset.obPrivate) : set.delete(el.dataset.obPrivate);
  } else if (el.name === "ob-read") {
    ob.read = el.value; ob.readChosen = true; drawOnboarding();
  } else if (el.name === "ob-vault") {
    ob.vault = el.value; drawOnboarding();
  } else if (el.name === "ob-lib") {
    ob.library = el.value;
  }
});

// --- start --------------------------------------------------------------------------------

try {
  const saved = localStorage.getItem("refs-theme");
  if (saved) document.documentElement.dataset.theme = saved;
} catch {}
// Links into the app: /?q=blp opens with that search, /?add=<link> opens the
// add bar with the link filled in (Enter confirms). Used by the Raycast quicklinks.
const params = new URLSearchParams(location.search);
if (params.get("q")) {
  state.query = params.get("q").trim();
  $("q").value = state.query;
}
applyLayout();
loadSidebar();
search();
maybeOnboard();

// --- following the folder watcher -----------------------------------------------------

// The server follows Dropbox (a PDF saved into papers-dump in Finder is read
// and renamed on its own). Every few seconds, ask whether anything changed and
// reload the list if so, keeping the selected row. Not while you are typing
// or editing, so the list never shifts under you; it catches up afterwards.
let seenGeneration = null;
let noticeDismissed = null;       // the size of the held batch you said "not now" to

// A large batch of new PDFs waits for your OK; the monthly limit can stop
// reading (budget.py). Both are said in the footer until dealt with.
function renderNotice(status) {
  const held = status.held;
  let html = "";
  if (held && noticeDismissed !== held.pdfs) {
    const cost = held.dollars < 0.01 ? "under $0.01" : `about $${held.dollars.toFixed(2)}`;
    html = `${held.pdfs.toLocaleString()} new PDFs to read (${cost}):
      <button class="act" data-notice="read">read them</button>
      <button class="act" data-notice="later" data-pdfs="${held.pdfs}">not now</button>`;
  } else if (status.limit_reached) {
    html = `this month's reading limit is reached; new PDFs wait.
      <button class="act" data-notice="settings">raise it</button>`;
  }
  $("notice").innerHTML = html;
}

// --- the watcher's status: header chip, progress bar, details panel ----------------------

let watchStatus = null;           // the last /api/changes answer (null: no answer)

const STAGES = {
  scanning: "scanning folders", indexing: "updating the search index",
  reading: "reading with Claude", filing: "filing and renaming",
};
const OUTCOME = {   // glyph and word for each entry of the activity list
  read: ["■", "read"], review: ["◧", "review"], failed: ["✗", "failed"],
  skipped: ["·", "skipped"], scan: ["~", "folders"],
};
const fileName = (path) => path?.split("/").pop() ?? "";
const clock = (t) => new Date(t * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hourCycle: "h23" });
function span(seconds) {
  if (seconds < 1) return "under 1 s";
  seconds = Math.round(seconds);
  if (seconds < 60) return `${seconds} s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)} min ${seconds % 60 ? `${seconds % 60} s` : ""}`.trim();
  return `${Math.floor(seconds / 3600)} h ${Math.round((seconds % 3600) / 60)} min`;
}

// How far the stage has come, and a guess at how long the rest takes: the
// average time per file so far, times the files left (after 2 files, so the
// first slow one does not skew it).
function stageNumbers(p, now) {
  const counted = p.total > 0 && ["scanning", "reading"].includes(p.stage);
  const share = counted ? Math.min(1, p.done / p.total) : null;
  const took = p.since ? now - p.since : 0;
  const left = counted && p.done >= 2 ? (took / p.done) * (p.total - p.done) : null;
  return { share, took, left };
}

function renderWatch(status) {
  watchStatus = status;
  const chip = $("watch"), bar = $("watchbar");
  const p = status?.progress;
  chip.classList.toggle("busy", Boolean(p));
  chip.classList.toggle("off", !status?.watching);
  bar.hidden = !p;
  if (!status) {
    chip.innerHTML = `<span class="dot">○</span> no answer`;
    chip.title = "the refs server does not answer";
  } else if (!status.watching) {
    chip.innerHTML = `<span class="dot">○</span> not watching`;
    chip.title = "this refs does not follow your folders (--no-watch, or another refs already does)";
  } else if (p) {
    const count = p.total && ["scanning", "reading"].includes(p.stage) ? ` ${p.done}/${p.total}` : " …";
    chip.innerHTML = `<span class="dot">◐</span> ${esc(p.stage)}${count}`;
    chip.title = (p.file ? `${fileName(p.file)}\n` : "") + "click for details (w)";
    const { share } = stageNumbers(p, status.now ?? Date.now() / 1000);
    bar.classList.toggle("unknown", share === null);
    bar.firstElementChild.style.width = `${Math.round((share ?? 0) * 100)}%`;
  } else if (status.pending) {
    chip.innerHTML = `<span class="dot">◌</span> changes seen`;
    chip.title = "waiting for the folders to settle, then a pass runs";
  } else {
    chip.innerHTML = `<span class="dot">●</span> watching`;
    chip.title = `following ${(status.roots ?? []).join(", ")}` +
      (status.last_pass ? `; last pass ${status.last_pass.slice(11, 16)}` : "") + "\nclick for details (w)";
  }
  if (!$("watchpanel").hidden) renderWatchPanel();
}

function renderWatchPanel() {
  const s = watchStatus, panel = $("watchpanel");
  if (!s) { panel.innerHTML = `<div>The refs server does not answer.</div>`; return; }
  if (!s.watching) {
    panel.innerHTML = `<div class="label">Watcher</div><div>Not watching. This refs was started
      with --no-watch, or another refs (the login service) already follows your folders.
      R rescans by hand.</div>`;
    return;
  }
  const reads = { all: "reads every new PDF", dump: `reads new PDFs in ${esc(state.dump)}`,
                  none: "reads nothing on its own" }[s.auto_process] ?? "";
  let html = `<div class="label">Watcher</div>
    <div>following ${esc((s.roots ?? []).join(", "))}; ${reads}</div>`;
  const p = s.progress;
  if (p) {
    const { share, took, left } = stageNumbers(p, s.now ?? Date.now() / 1000);
    const count = share !== null ? `: ${p.done.toLocaleString()} of ${p.total.toLocaleString()}` : " …";
    const timing = [`${span(took)} so far`, left !== null && `about ${span(left)} left`].filter(Boolean).join(", ");
    html += `<div class="label">Now</div>
      <div>${esc(STAGES[p.stage] ?? p.stage)}${count} <span class="path">· ${timing}</span></div>
      <div class="ob-bar"><span style="width:${Math.round((share ?? 1) * 100)}%"></span></div>`;
    if (p.file) html += `<div class="now">${esc(fileName(p.file))}</div><div class="path">${esc(p.file)}</div>`;
    if (s.pass_started) html += `<div class="path">this pass began at ${clock(s.pass_started)}</div>`;
  } else if (s.pending) {
    html += `<div class="label">Now</div><div>changes seen; a pass starts once the folders are quiet for a few seconds</div>`;
  } else {
    html += `<div class="label">Now</div><div>idle${s.last_pass
      ? `; last pass at ${esc(s.last_pass.slice(11, 16))}${s.last_pass_seconds != null ? `, took ${span(s.last_pass_seconds)}` : ""}`
      : ""}</div>`;
  }
  html += `<div class="label">Recent</div>`;
  if (!s.recent?.length) {
    html += `<div class="path">nothing yet since refs started</div>`;
  } else {
    html += `<div class="wlog">` + s.recent.map((e) => {
      const [glyph, word] = OUTCOME[e.outcome] ?? ["·", e.outcome];
      const renamed = e.renamed_from ? `<div class="sub">renamed from ${esc(fileName(e.renamed_from))}</div>` : "";
      const issues = e.issues?.length ? `<div class="sub">! ${esc(e.issues.join("; "))}</div>` : "";
      const where = e.path ? `<div class="sub">${esc(e.path)}</div>` : "";
      return `<span class="t">${clock(e.at)}</span><span class="${e.outcome}">${glyph} ${word}</span>
        <span title="${esc(e.path ?? "")}">${esc(e.text)}${where}${renamed}${issues}</span>`;
    }).join("") + `</div>`;
  }
  panel.innerHTML = html;
}

function toggleWatchPanel(open = $("watchpanel").hidden) {
  $("watchpanel").hidden = !open;
  if (open) { $("colmenu").hidden = true; renderWatchPanel(); checkChanges(); }
}
$("watch").addEventListener("click", (event) => { event.stopPropagation(); toggleWatchPanel(); });
$("watchpanel").addEventListener("click", (event) => event.stopPropagation());

$("notice").addEventListener("click", async (event) => {
  const button = event.target.closest("[data-notice]");
  if (!button) return;
  if (button.dataset.notice === "read") {
    await post("/api/read/approve");
    $("notice").innerHTML = "";
    say("reading the new PDFs in the background …", 8000);
  } else if (button.dataset.notice === "later") {
    noticeDismissed = Number(button.dataset.pdfs);
    $("notice").innerHTML = "";
  } else if (button.dataset.notice === "settings") {
    state.context = null; state.tag = null; state.collection = null; state.view = "settings";
    markSidebar(); search();
  }
});

async function checkChanges() {
  if (document.hidden) return;
  let status;
  try { status = await get("/api/changes"); } catch { renderWatch(null); return; }
  renderNotice(status);
  renderWatch(status);
  if (seenGeneration === null) { seenGeneration = status.generation; return; }
  if (status.generation === seenGeneration) return;
  if (document.activeElement?.matches?.("input, textarea, select")) return;
  seenGeneration = status.generation;
  if (status.message) say(status.message, 8000);
  loadSidebar();
  search(state.items[state.selected]?.id ?? null);
}
// Every 5 s; every second while a pass runs or the watcher panel is open.
(async function followWatcher() {
  await checkChanges();
  const busy = watchStatus?.progress || watchStatus?.pending || !$("watchpanel").hidden;
  setTimeout(followWatcher, busy ? 1000 : 5000);
})();
document.addEventListener("visibilitychange", checkChanges);
if (params.get("add")) {
  setAdding(true);
  $("q").value = params.get("add").trim();
  say("press Enter to add it");
}
