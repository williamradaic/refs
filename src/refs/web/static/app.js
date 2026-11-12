// refs: the page's behaviour. No framework: the state is one object, and each
// change re-renders the pane it affects.

const state = {
  query: "",          // what is typed in the search bar
  view: "all",        // sidebar preset: all | unread | review | wanted
  context: null,      // selected course or project key, e.g. "coursework/2026-fall/220a"
  tag: null,          // selected field tag
  scope: "library",   // "library" = papers only, "all" = course material too
  items: [],          // current search results
  selected: 0,        // index of the highlighted row
  adding: false,      // true while the bar is in "add a paper" mode
};

const $ = (id) => document.getElementById(id);
const esc = (text) => String(text ?? "").replace(/[&<>"]/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const GLYPH = { verified: "●", confirmed: "✓", imported: "◆", extracted: "◐",
  "needs-review": "!", "no-file": "○", classified: "·" };

// --- columns ---------------------------------------------------------------------
// Each column knows its header label, default width (in characters; null = takes
// the remaining space), how to sort, and what to show.
const last = "￿";      // sorts after every real string: empty values go to the end
const COLUMNS = {
  st:      { label: "",        width: 2,  align: "center", sort: (i) => i.status ?? last,
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
COLUMNS.st.name = "status"; COLUMNS.abbr.name = "journal abbreviation";
const DEFAULT_LAYOUT = { order: ["st", "authors", "year", "title", "abbr", "venue", "added", "modified"],
  hidden: [], widths: {}, sort: null, side: null, detail: null };

// Column order, widths, sort and pane sizes are remembered in this browser.
function loadLayout() {
  try {
    const saved = JSON.parse(localStorage.getItem("refs-layout"));
    if (saved?.order) {
      // Keep the saved order, drop columns that no longer exist, and append
      // any column added to the app since the layout was saved.
      const order = saved.order.filter((id) => id in COLUMNS);
      for (const id of DEFAULT_LAYOUT.order) if (!order.includes(id)) order.push(id);
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
    ? "minmax(14ch, 1fr)" : `${layout.widths[id] ?? COLUMNS[id].width}ch`).join(" "));
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
  const nav = (label, count, attrs, sub = false) =>
    `<div class="nav${sub ? " sub" : ""}" ${attrs}><span>${esc(label)}</span><span class="n">${count}</span></div>`;

  let html = `<div class="label">Library</div>`;
  html += nav("all papers", data.presets.all, `data-view="all"`);
  html += nav("unread", data.presets.unread, `data-view="unread"`);
  html += nav("needs review", data.presets.review, `data-view="review"`);
  html += nav("no pdf yet", data.presets.wanted, `data-view="wanted"`);
  // PDFs that are on disk but were never read (an interrupted add, or a file
  // that arrived through Finder). Shown only when there are some.
  if (data.presets.unprocessed) html += nav("not identified", data.presets.unprocessed, `data-view="unprocessed"`);

  // Contexts arrive as keys like "coursework/2026-fall/220a"; group by area,
  // and within coursework by term (newest first).
  const areas = {};
  for (const { key, count } of data.contexts) {
    const [area, ...rest] = key.split("/");
    (areas[area] ??= []).push({ key, rest, count });
  }
  for (const area of ["dump", "research", "coursework", "projects", "ra", "referee"]) {
    if (!areas[area]) continue;
    if (area === "dump") {
      html += nav("dump", areas.dump[0].count, `data-context="dump"`);
      continue;
    }
    html += `<div class="label">${esc(area)}</div>`;
    let term = null;
    const entries = areas[area].sort((a, b) => (b.rest[0] ?? "").localeCompare(a.rest[0] ?? ""));
    for (const { key, rest, count } of area === "coursework" ? entries : areas[area]) {
      if (area === "coursework" && rest.length > 1 && rest[0] !== term) {
        term = rest[0];
        html += `<div class="group">${esc(term)}</div>`;
      }
      const label = rest[rest.length - 1] ?? area;
      html += nav(label, count, `data-context="${esc(key)}"`, area === "coursework" && rest.length > 1);
    }
  }
  const shown = new Set(["dump", "research", "coursework", "projects", "ra", "referee"]);
  const others = Object.keys(areas).filter((a) => !shown.has(a));
  if (others.length) html += `<div class="label">Other</div>`;
  for (const area of others) for (const { key, count } of areas[area]) html += nav(key, count, `data-context="${esc(key)}"`);
  html += `<div class="label">Fields</div>`;
  for (const [tag, count] of data.fields) html += nav(tag, count, `data-tag="${esc(tag)}"`);
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

async function search(keepId = null) {
  // The sidebar selection becomes ordinary filters in front of the typed query.
  const parts = [];
  if (state.context) parts.push(state.context === "dump" ? "area:dump" : `in:${state.context}`);
  if (state.tag) parts.push(`tag:${state.tag.replace(/\s+/g, "-")}`);
  if (state.query) parts.push(state.query);
  const url = `/api/search?q=${encodeURIComponent(parts.join(" "))}&view=${state.view}&scope=${state.scope}`;
  const data = await get(url);
  state.raw = data.items;                 // the server's order, kept for "no sort"
  if (data.error) say(data.error);
  sortItems();
  const kept = keepId ? state.items.findIndex((i) => i.id === keepId) : -1;
  state.selected = Math.max(0, kept);
  renderList();
  renderDetail();
}

function sortItems() {
  state.items = [...(state.raw ?? [])];
  const chosen = layout.sort;
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
    const grip = column.width === null ? "" : `<b class="grip" data-col="${id}"></b>`;
    // The label sits on the same edge as the column's contents; the sort arrow
    // goes on the inner side, so the label itself stays flush with the data.
    const label = `<span class="hl">${esc(column.label)}</span>`;
    const mark = arrow ? `<i>${arrow}</i>` : "";
    return `<span class="hc ${column.align ?? "left"}" draggable="true" data-col="${id}"
      title="click to sort, drag to move">${column.align === "right" ? mark + label : label + mark}${grip}</span>`;
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
    const cls = ["row", i === state.selected ? "sel" : "", item.read_status === "read" ? "read" : ""].join(" ");
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
    layout.widths[resizing.id] = Math.max(2, Math.round(resizing.startWidth + delta));
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
  let work = null, bibtex = "";
  if (item.work_id) {
    const data = await get(`/api/work/${item.work_id}`);
    if (token !== detailToken) return;
    work = data.work; bibtex = data.bibtex;
  }
  const meta = work?.meta ?? {};
  const authors = meta.authors?.length
    ? meta.authors.map((a) => [a.given, a.family].filter(Boolean).join(" ")).join(", ")
    : (item.authors ?? []).join(", ");
  const issue = [meta.volume, meta.issue && `(${meta.issue})`].filter(Boolean).join("");
  const where = [item.venue, item.year, issue, meta.pages].filter(Boolean).join(" · ");
  const status = item.status ?? "classified";

  let html = `<h1>${esc(item.title ?? "untitled")}</h1>`;
  if (authors) html += `<p class="who">${esc(authors)}</p>`;
  if (where) html += `<p class="where">${esc(where)}</p>`;

  html += `<div class="label">Record</div><dl class="meta">`;
  html += `<dt>status</dt><dd><span class="st ${status}">${GLYPH[status] ?? "·"}</span> ${esc(status)}${
    meta.source ? ` <span class="badge">· ${esc(meta.source === "pdf" ? "read from the pdf" : "from " + meta.source)}</span>` : ""}</dd>`;
  if (item.citekey) html += `<dt>citekey</dt><dd>${esc(item.citekey)}</dd>`;
  if (meta.doi) html += `<dt>doi</dt><dd><a href="https://doi.org/${esc(meta.doi)}" target="_blank" rel="noreferrer">${esc(meta.doi)}</a></dd>`;
  html += `<dt>kind</dt><dd>${esc(item.kind)}</dd>`;
  if (item.read_status) html += `<dt>read</dt><dd>${esc(item.read_status)}</dd>`;
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
  if (work?.user?.notes) html += `<div class="label">Notes</div><p class="notes">${esc(work.user.notes)}</p>`;

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
  if (!item.locations.length) html += `<p class="where">no pdf yet. Save it into papers-dump and it attaches itself.</p>`;
  for (const loc of item.locations) {
    const place = [loc.context, loc.topic ?? loc.section].filter(Boolean).join(" · ");
    html += `<div class="loc"><div>${esc(place)}</div>
      <div class="path">${esc(loc.path.split("/").pop())}</div>
      <div class="acts">
        <a class="act" href="/file?path=${encodeURIComponent(loc.path)}" target="_blank">open</a>
        <button class="act" data-open="${esc(loc.path)}">preview.app</button>
        <button class="act" data-reveal="${esc(loc.path)}">finder</button>
      </div></div>`;
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
    </div>`;
  }
  $("detail").innerHTML = html;
  $("detail").dataset.bibtex = bibtex;
  currentWork = work;
}

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
  else if (el.dataset.act === "edit") showEditor();
  else if (el.dataset.act === "cancel-edit") $("editor").innerHTML = "";
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
  if (name === "read" && item.work_id) {
    const next = item.read_status === "read" ? "unread" : "read";
    await post(`/api/work/${item.work_id}/read`, { status: next });
    item.read_status = next;
    const keep = state.selected;
    renderList(); select(keep, false); loadSidebar();
  }
}

// --- search bar, including "add" mode -------------------------------------------------

function setAdding(on) {
  state.adding = on;
  $("prompt").textContent = on ? "+" : "/";
  $("q").value = on ? "" : state.query;
  $("q").placeholder = on
    ? "add a paper: paste a URL, DOI, arXiv id or NBER number, then Enter"
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
    if (!state.adding) { $("q").blur(); return; }
    const thing = $("q").value.trim();
    if (!thing) return;
    setAdding(false); $("q").blur();
    say(`adding ${thing} …`, 0);
    follow(post("/api/add", { thing }), thing);
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
    say(`lost contact with refs while adding ${label}. The file, if saved, is in papers-dump.`, 0);
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

// --- keyboard ---------------------------------------------------------------------------

const HELP = `keys

  j / k  or  ↓ / ↑     move
  g / G                top / bottom
  /                    search          esc   clear
  enter  or  o         open the pdf in a new tab
  p                    open in Preview
  f                    show in Finder
  y                    copy citekey
  b                    copy BibTeX
  r                    toggle read / unread
  a                    add a paper (URL, DOI, arXiv id, NBER number)
  m                    show course material too
  R                    rescan Dropbox
  t                    light / dark

search filters

  author:saez   year:2019   course:220a   project:bets
  tag:theory    kind:book   status:needs-review   topic:demand`;

document.addEventListener("keydown", async (event) => {
  if ($("help").open) { $("help").close(); return; }
  if (event.target.matches?.("input, textarea, select")) {
    if (event.key === "Escape" && event.target !== $("q")) $("editor").innerHTML = "";
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
  else if (key === "y") act("citekey");
  else if (key === "b") act("bibtex");
  else if (key === "r") act("read");
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
