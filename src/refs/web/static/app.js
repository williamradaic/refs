// refs: the page's behaviour. No framework: the state is one object, and each
// change re-renders the pane it affects.

const state = {
  query: "",          // what is typed in the search bar
  view: "all",        // sidebar preset: all | queue | unread | reading | read | review | wanted | renames | copies
  context: null,      // selected course or project key, e.g. "coursework/2026-fall/220a"
  tag: null,          // selected field tag
  scope: "library",   // "library" = papers only, "all" = course material too
  items: [],          // current search results
  selected: 0,        // index of the highlighted row
  adding: false,      // true while the bar is in "add a paper" mode
  picked: new Set(),  // renames view: paths ticked for renaming
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
  // Dates are YYYY-MM-DD, so sorting them as text is sorting them in time.
  added:   { label: "added",    width: 10, cls: "da", sort: (i) => i.added ?? "",
             text: (i) => i.added ?? "" },
  modified: { label: "modified", width: 10, cls: "da", sort: (i) => i.modified ?? "",
             text: (i) => i.modified ?? "" },
};
COLUMNS.st.name = "status"; COLUMNS.abbr.name = "journal abbreviation"; COLUMNS.rd.name = "queue / reading";
const DEFAULT_LAYOUT = { order: ["rd", "st", "authors", "year", "title", "abbr", "venue", "added", "modified"],
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
  const data = await get("/api/sidebar");
  state.dump = data.dump || state.dump;
  const nav = (label, count, attrs, sub = false) =>
    `<div class="nav${sub ? " sub" : ""}" ${attrs}><span>${esc(label)}</span><span class="n">${count}</span></div>`;

  let html = `<div class="label">Library</div>`;
  html += nav("all papers", data.presets.all, `data-view="all"`);
  html += nav("queue", data.presets.queue, `data-view="queue"`);
  html += nav("unread", data.presets.unread, `data-view="unread"`);
  html += nav("reading", data.presets.reading, `data-view="reading"`);
  html += nav("read", data.presets.read, `data-view="read"`);
  html += nav("no pdf yet", data.presets.wanted, `data-view="wanted"`);
  // Things that want a decision from you.
  html += `<div class="label">Review</div>`;
  html += nav("needs review", data.presets.review, `data-view="review"`);
  html += nav("renames", data.presets.renames, `data-view="renames"`);
  html += nav("copies", data.presets.copies, `data-view="copies"`);
  // PDFs that are on disk but were never read (an interrupted add, or a file
  // that arrived through Finder). Shown only when there are some.
  if (data.presets.unprocessed) html += nav("not identified", data.presets.unprocessed, `data-view="unprocessed"`);

  // Contexts arrive as keys like "coursework/2026-fall/220a", each with its
  // area and term. The layout (contexts.py) says which areas get a heading of
  // their own, in order; within an area that has terms, group by term, newest
  // first.
  const areas = {};
  for (const { key, count, area, term } of data.contexts) {
    const name = area ?? key.split("/")[0];
    const rest = key.split("/").slice(1);          // the key below the area
    (areas[name] ??= []).push({ key, rest, count, term });
  }
  const listed = ["dump", ...data.areas];
  for (const area of listed) {
    if (!areas[area]) continue;
    if (area === "dump") {
      html += nav(state.dump, areas.dump[0].count, `data-context="dump"`);
      continue;
    }
    html += `<div class="label">${esc(area)}</div>`;
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
  }
  const shown = new Set(listed);
  const others = Object.keys(areas).filter((a) => !shown.has(a));
  if (others.length) html += `<div class="label">Other</div>`;
  for (const area of others) for (const { key, count } of areas[area]) html += nav(key, count, `data-context="${esc(key)}"`);
  html += `<div class="label">Fields</div>`;
  for (const [tag, count] of data.fields) html += nav(tag, count, `data-tag="${esc(tag)}"`);
  html += `<div class="label">refs</div>`;
  html += `<div class="nav" data-view="settings"><span>settings</span><span class="n">,</span></div>`;
  $("side").innerHTML = html;
  markSidebar();
}

function markSidebar() {
  for (const el of $("side").querySelectorAll(".nav")) {
    const on = el.dataset.context ? el.dataset.context === state.context
      : el.dataset.tag ? el.dataset.tag === state.tag
      : !state.context && !state.tag && el.dataset.view === state.view;
    el.classList.toggle("on", on);
  }
}

$("side").addEventListener("click", (event) => {
  const el = event.target.closest(".nav");
  if (!el) return;
  state.context = el.dataset.context ?? null;
  state.tag = el.dataset.tag ?? null;
  state.view = el.dataset.view ?? "all";
  markSidebar();
  search();
});

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
  if (REVIEW_VIEWS[state.view] && !state.context && !state.tag) return REVIEW_VIEWS[state.view](token);
  // The sidebar selection becomes ordinary filters in front of the typed query.
  const parts = [];
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
  const kept = keepId ? state.items.findIndex((i) => i.id === keepId) : -1;
  state.selected = Math.max(0, kept);
  renderList();
  renderDetail();
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
    $("list").innerHTML = renderHead() + `<div class="empty">nothing here.</div>`;
    return;
  }
  // Inside a course, with no search words and no chosen sort, show the
  // reading-list structure: a heading whenever the topic changes.
  const grouped = state.context && !state.query && !layout.sort;
  let html = renderHead(), lastTopic;
  items.forEach((item, i) => {
    const loc = item.locations.find((l) => l.context === state.context) ?? item.locations[0];
    if (grouped && loc && loc.topic !== lastTopic) {
      lastTopic = loc.topic;
      const number = loc.topic_order != null ? String(loc.topic_order).padStart(2, "0") + " " : "";
      html += `<div class="topic">${esc(number + (loc.topic ?? loc.section ?? "unfiled"))}</div>`;
    }
    const cls = ["row", i === state.selected ? "sel" : ""].join(" ");
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
  layout.sort = current?.col !== id ? { col: id, dir: "asc" }
    : current.dir === "asc" ? { col: id, dir: "desc" } : null;
  saveLayout();
  const keep = state.items[state.selected]?.id;
  sortItems();
  state.selected = Math.max(0, state.items.findIndex((i) => i.id === keep));
  renderList();
});

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
document.addEventListener("click", () => { $("colmenu").hidden = true; });

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
  renderDetail();
}

$("list").addEventListener("click", (event) => {
  const row = event.target.closest(".row");
  if (row) select(Number(row.dataset.i), false);
});
$("list").addEventListener("dblclick", (event) => { if (event.target.closest(".row")) act("open"); });

// --- detail ------------------------------------------------------------------------

let detailToken = 0;
async function renderDetail() {
  const item = state.items[state.selected];
  if (!item) { $("detail").innerHTML = ""; return; }
  const token = ++detailToken;        // ignore a slow answer for a row we already left

  // Full metadata (first names, volume, pages, BibTeX) exists only for works.
  let work = null, bibtex = "", noteData = null;
  if (item.work_id) {
    const data = await get(`/api/work/${item.work_id}`);
    if (token !== detailToken) return;
    work = data.work; bibtex = data.bibtex; noteData = data.note;
  }
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
  if (item.added) html += `<dt>added</dt><dd>${esc(item.added)}</dd>`;
  if (item.modified) html += `<dt>modified</dt><dd>${esc(item.modified)}</dd>`;
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
  if (!item.locations.length) html += `<p class="where">no pdf yet. Save it into ${esc(state.dump)} and it attaches itself.</p>`;
  for (const loc of item.locations) {
    const place = [loc.context, loc.topic ?? loc.section].filter(Boolean).join(" · ");
    html += `<div class="loc"><div>${esc(place)}</div>
      <div class="path">${esc(loc.path.split("/").pop())}</div>
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
  const form = new FormData(event.target), fields = {};
  for (const [name, value] of form) fields[name] = String(value).trim();
  fields.authors = parseAuthors(fields.authors);
  if (!fields.authors.length) delete fields.authors;
  if (fields.kind === "paper") fields.type = fields.venue ? "journal-article" : "posted-content";
  changeWork("edit", { fields }, "saved your corrections");
});

$("detail").addEventListener("click", (event) => {
  const el = event.target;
  if (el.dataset.identify) {
    const name = el.dataset.identify.split("/").pop();
    say(`adding ${name} …`, 0);
    follow(post("/api/identify", { path: el.dataset.identify }), name);
  }
  else if (el.dataset.open) post("/api/open", { path: el.dataset.open });
  else if (el.dataset.reveal) post("/api/open", { path: el.dataset.reveal, reveal: true });
  else if (el.dataset.fileto) openPicker(el.dataset.fileto, "move");
  else if (el.dataset.copyto) openPicker(el.dataset.copyto, "copy");
  else if (el.dataset.folder) {
    const box = el.closest(".picker");
    fileTo(box.dataset.for, el.dataset.folder, box.dataset.mode);
  }
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
    state.tag = el.dataset.tag; state.context = null; state.view = "all";
    markSidebar(); search();
  }
});

// --- actions (shared by keys and buttons) -------------------------------------------

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
    ? "add a paper: paste a URL, DOI, arXiv id or NBER number, then Enter (shift+Enter: add and queue)"
    : "search   author:saez   course:220a   tag:theory   year:2019";
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
    const thing = $("q").value.trim();
    if (!thing) return;
    const queue = event.shiftKey;
    setAdding(false); $("q").blur();
    say(`adding ${thing}${queue ? " to the queue" : ""} …`, 0);
    follow(post("/api/add", { thing, queue }), thing);
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
      if (job.state === "running") { say(`adding ${label} … ${job.seconds}s`, 0); continue; }
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
    const upload = fetch(`/api/upload?name=${encodeURIComponent(file.name)}`, {
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
    (canvas-sync, overleaf, Git repositories).</p>
    <p class="where" style="margin-top:8px">Folders shared with coauthors (e.g. research/bets) are
    renamed for them too, through Dropbox.</p>`;
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
  say(`restored ${result.restored} names`);
  loadSidebar(); search();
}

// --- review: identical copies -------------------------------------------------------------

// The same PDF in more than one place. Often on purpose (a reading for a course
// and for a project); otherwise trash the extra copy. The last copy cannot be trashed.
// Sets marked "keep all" are listed at the bottom, folded, where they can be un-kept.
function copySet(item, kept) {
  const name = item.title ?? item.locations[0].path.split("/").pop();
  const who = item.authors?.length ? `${authorsShort(item.authors)} (${item.year ?? "n.d."}) ` : "";
  const button = kept ? `<button class="act" data-unkeep="${esc(item.hash)}">un-keep</button>`
                      : `<button class="act" data-keep="${esc(item.hash)}">keep all</button>`;
  let html = `<div class="copyset"><div class="topic"><span>${esc(who + name)}</span>${button}</div>`;
  for (const loc of item.locations) {
    html += `<div class="copy"><span class="path">${esc(loc.path)}</span>
      <span class="acts"><button class="act" data-reveal="${esc(loc.path)}">finder</button>
      ${kept ? "" : `<button class="act" data-trash="${esc(loc.path)}">trash</button>`}</span></div>`;
  }
  return html + `</div>`;
}

async function showCopies(token) {
  const data = await get("/api/copies");
  if (token !== listToken) return;
  state.items = [];
  $("count").textContent = `${data.items.length} sets of copies`;
  let html = data.items.length ? "" : `<div class="empty">no file is stored twice.</div>`;
  html += data.items.map((item) => copySet(item, false)).join("");
  if (data.kept.length) {
    html += `<details class="kept"><summary class="topic">kept on purpose (${data.kept.length})</summary>
      ${data.kept.map((item) => copySet(item, true)).join("")}</details>`;
  }
  $("list").innerHTML = html;
  $("detail").innerHTML = `<div class="label">Copies</div>
    <p>The same PDF, byte for byte, stored in more than one place.</p>
    <p class="where" style="margin-top:8px"><b>trash</b> moves that copy to the macOS Trash
    (drag it back to restore). <b>keep all</b> says the copies are on purpose and moves the
    set to <b>kept on purpose</b> at the bottom, where <b>un-keep</b> brings it back.</p>`;
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

const REVIEW_VIEWS = { renames: showRenames, copies: showCopies, settings: showSettings };

$("list").addEventListener("change", (event) => {
  const path = event.target.dataset?.path;
  if (!path) return;
  event.target.checked ? state.picked.add(path) : state.picked.delete(path);
  $("npicked").textContent = state.picked.size;
});

$("list").addEventListener("click", async (event) => {
  const el = event.target.closest("button");
  if (!el) return;
  const boxes = [...$("list").querySelectorAll("input[data-path]")];
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
  else if (el.dataset.apply) await applyRenames();
  else if (el.dataset.undo) await undoBatch(Number(el.dataset.undo));
  else if (el.dataset.reveal) post("/api/open", { path: el.dataset.reveal, reveal: true });
  else if (el.dataset.keep) {
    await post("/api/copies/keep", { hash: el.dataset.keep });
    say("kept: these copies are on purpose (un-keep under \"kept on purpose\")");
    loadSidebar(); search();
  }
  else if (el.dataset.unkeep) {
    await post("/api/copies/unkeep", { hash: el.dataset.unkeep });
    say("back in the list to review");
    loadSidebar(); search();
  }
  else if (el.dataset.trash) {
    if (!confirm(`Move this copy to the Trash?\n\n${el.dataset.trash}`)) return;
    const response = await fetch("/api/trash", { method: "POST",
      headers: { "Content-Type": "application/json", "X-Refs": "1" },
      body: JSON.stringify({ path: el.dataset.trash }) });
    say(response.ok ? "moved to the Trash" : (await response.json()).detail);
    loadSidebar(); search();
  }
  const counter = $("npicked");
  if (counter) counter.textContent = state.picked.size;
});

// --- file to… / copy to… --------------------------------------------------------------------------------

// A filter box over every folder refs knows (where PDFs already are, and the
// folders above them). Type words of the path, then Enter or click.
// `mode` is "move" (file to…) or "copy" (copy to…: the original stays put).
let folderList = null;
async function openPicker(path, mode = "move") {
  const box = [...$("detail").querySelectorAll(".picker")].find((el) => el.dataset.for === path);
  if (!box) return;
  box.dataset.mode = mode;
  // The box takes the keyboard at once, before the folder list has loaded:
  // otherwise quick typing would land on the page's single-key shortcuts.
  const verb = mode === "copy" ? "copy into" : "file into";
  box.innerHTML = `<input class="pick" placeholder="${verb}: type to find a folder, e.g. 220a readings demand" spellcheck="false" autocomplete="off">
    <div class="choices"><div class="where">loading folders …</div></div>`;
  const input = box.querySelector("input");
  input.focus();
  folderList ??= (await get("/api/folders")).folders;
  const draw = () => {
    const words = input.value.toLowerCase().split(/\s+/).filter(Boolean);
    const hits = folderList.filter((f) => words.every((w) => f.toLowerCase().includes(w))).slice(0, 14);
    // A first folder every choice shares ("phd/") says nothing: leave it out.
    const first = folderList[0]?.split("/")[0];
    const shared = first && folderList.every((f) => f.startsWith(first + "/")) ? first.length + 1 : 0;
    box.querySelector(".choices").innerHTML = hits.map((f) =>
      `<div class="choice" data-folder="${esc(f)}">${esc(f.slice(shared))}</div>`).join("")
      || `<div class="where">no folder matches</div>`;
  };
  input.addEventListener("input", draw);
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      const first = box.querySelector(".choice");
      if (first) fileTo(path, first.dataset.folder, mode);
    }
  });
  draw();
  input.focus();
}

async function fileTo(path, folder, mode = "move") {
  const copying = mode === "copy";
  const response = await fetch(copying ? "/api/copy" : "/api/move", { method: "POST",
    headers: { "Content-Type": "application/json", "X-Refs": "1" },
    body: JSON.stringify({ path, folder }) });
  const result = await response.json();
  if (!response.ok) { say(result.detail ?? (copying ? "could not copy it" : "could not move it")); return; }
  folderList = null;                          // the new location may add a folder
  say(copying ? `copied into ${folder}; the original stays. undo (trashes the copy): refs rename --undo`
              : `filed into ${folder}. undo: refs rename --undo`, 9000);
  loadSidebar();
  search(state.items[state.selected]?.id ?? null);
}

// --- keyboard ---------------------------------------------------------------------------

const HELP = `keys

  j / k  or  ↓ / ↑     move
  g / G                top / bottom
  /                    search          esc   clear
  enter  or  o         open the pdf in a new tab
  p                    open in Preview
  f                    show in Finder
  F                    file to… (move the PDF into a course or project folder)
  C                    copy to… (same, but the original stays where it is)
  y                    copy citekey
  b                    copy BibTeX
  r                    toggle read / unread
  q                    put on the queue / take off (▸ in the list)
  n                    write a note (saved to Obsidian; esc leaves the box)
  a                    add a paper (URL, DOI, arXiv id, NBER number); shift+Enter also queues it
  m                    show course material too
  R                    rescan Dropbox
  ,                    settings (folders, notes, AI, API key)
  t                    light / dark

review (sidebar)

  renames              tick files, then "rename selected"; undo is offered after
  copies               the same PDF in several places: trash a copy or keep all

bookmarklet            open /bookmarklet and drag "+ refs" to the bookmarks bar

search filters

  author:saez   year:2019   course:220a   project:bets
  tag:theory    kind:book   status:needs-review   topic:demand
  read:read     read:reading   read:unread   queued:yes

reading

  your highlights and notes in a PDF count: marks on 30% of its pages (at
  least 5) make it read (●), marks on 2 or more pages make it started (◐).
  What you set with r always wins. ▸ = on the queue.`;

document.addEventListener("keydown", async (event) => {
  if ($("help").open) { $("help").close(); return; }
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
  if (event.metaKey || event.ctrlKey || event.altKey) return;
  const key = event.key;
  const moves = { j: 1, ArrowDown: 1, k: -1, ArrowUp: -1 };
  if (key in moves) { event.preventDefault(); select(state.selected + moves[key]); }
  else if (key === "g") select(0);
  else if (key === "G") select(state.items.length - 1);
  else if (key === "/") { event.preventDefault(); setAdding(false); $("q").focus(); $("q").select(); }
  else if (key === "a") { event.preventDefault(); setAdding(true); }
  else if (key === "Enter" || key === "o") act("open");
  else if (key === "p") act("preview");
  else if (key === "f") act("reveal");
  else if (key === "F" || key === "C") {
    event.preventDefault();
    const p = state.items[state.selected]?.locations[0]?.path;
    if (p) openPicker(p, key === "C" ? "copy" : "move");
  }
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
  else if (key === ",") {
    state.context = null; state.tag = null; state.view = "settings";
    markSidebar(); search();
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

// --- following the folder watcher -----------------------------------------------------

// The server follows Dropbox (a PDF saved into papers-dump in Finder is read
// and renamed on its own). Every few seconds, ask whether anything changed and
// reload the list if so, keeping the selected row. Not while you are typing
// or editing, so the list never shifts under you; it catches up afterwards.
let seenGeneration = null;
async function checkChanges() {
  if (document.hidden) return;
  let status;
  try { status = await get("/api/changes"); } catch { return; }
  if (seenGeneration === null) { seenGeneration = status.generation; return; }
  if (status.generation === seenGeneration) return;
  if (document.activeElement?.matches?.("input, textarea, select")) return;
  seenGeneration = status.generation;
  if (status.message) say(status.message, 8000);
  loadSidebar();
  search(state.items[state.selected]?.id ?? null);
}
setInterval(checkChanges, 5000);
document.addEventListener("visibilitychange", checkChanges);
if (params.get("add")) {
  setAdding(true);
  $("q").value = params.get("add").trim();
  say("press Enter to add it");
}
