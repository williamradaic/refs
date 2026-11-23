// Collection workspace header: reading progress, per-collection filter pills, and direct actions.

import { esc, get, post, say, copyText, proxyUrl } from "./api.js";

const PILLS = [
  ["all", "all"],
  ["queued", "queued"],
  ["unread", "unread"],
  ["reading", "reading"],
  ["skimmed", "skimmed"],
  ["read", "read"],
  ["wanted", "no pdf"],
];

export function filterCollectionItems(items, filter) {
  if (!filter || filter === "all") return items;
  if (filter === "queued") return items.filter((i) => Boolean(i.queued));
  if (filter === "wanted") return items.filter((i) => !i.locations?.length);
  return items.filter((i) => i.read_status === filter);
}

export function renderCollectionBar(state) {
  if (!state.collection) return "";
  const name = state.collection;
  const info = state.collectionInfo?.[name] ?? {};
  const all = state.collectionAll ?? state.raw ?? [];
  const total = all.length;
  const readCount = all.filter((i) => i.read_status === "read").length;
  const skimmedCount = all.filter((i) => i.read_status === "skimmed").length;
  const queuedCount = all.filter((i) => Boolean(i.queued)).length;
  const wantedCount = all.filter((i) => !i.locations?.length).length;
  const active = state.collectionFilter || "all";

  const counts = {
    all: total,
    queued: queuedCount,
    unread: all.filter((i) => i.read_status === "unread").length,
    reading: all.filter((i) => i.read_status === "reading").length,
    skimmed: skimmedCount,
    read: readCount,
    wanted: wantedCount,
  };

  const progress = total
    ? `${readCount}/${total} read` +
      (skimmedCount ? ` · ${skimmedCount} skimmed` : "") +
      (queuedCount ? ` · ${queuedCount} queued` : "")
    : "no papers yet";

  const badges = [
    info.term,
    info.shared ? "shared ⇄" : "",
    info.bib_file ? info.bib_file.split("/").pop() : "",
  ].filter(Boolean).join(" · ");

  const pills = PILLS.filter(([k]) => k === "all" || counts[k] > 0 || active === k).map(([k, label]) =>
    `<button class="pill${active === k ? " on" : ""}" data-cfilter="${k}">${esc(label)} <span class="n">${counts[k]}</span></button>`
  ).join("");

  const openProxyBtn = active === "wanted" && wantedCount > 0 && state.proxy
    ? `<button class="act" data-ws="proxy5" title="open up to 5 papers without PDFs through your library proxy">open next ${Math.min(5, wantedCount)} via proxy</button>`
    : "";

  return `<div class="wsbar">
    <div class="wstop">
      <span class="wstitle">#${esc(name)}</span>
      ${badges ? `<span class="badge">${esc(badges)}</span>` : ""}
      <span class="wsprog">${esc(progress)}</span>
    </div>
    <div class="wsacts">
      <button class="act" data-ws="add">+ add to #${esc(name)}</button>
      <button class="act" data-ws="import">import list/.bib</button>
      ${info.bib_file ? `<button class="act" data-ws="append-bib" title="append all papers in #${esc(name)} to ${esc(info.bib_file)}">append to .bib</button>` : ""}
      ${total ? `<button class="act" data-ws="bibtex">copy BibTeX</button>` : ""}
      ${info.folder ? `<button class="act" data-ws="folder">open folder</button>` : ""}
      ${openProxyBtn}
    </div>
    <div class="wspills">${pills}</div>
  </div>`;
}

export async function handleWorkspaceAction(action, state, hooks) {
  const name = state.collection;
  if (!name) return;
  if (action === "add") {
    hooks.setAdding(true);
    const input = document.getElementById("q");
    input.value = `#${name} `;
    input.setSelectionRange(0, 0);
    say(`paste a link, DOI, arXiv or NBER id before #${name}`);
  } else if (action === "import") {
    hooks.openImport(name);
  } else if (action === "bibtex") {
    copyText(
      get(`/api/collections/bibtex?name=${encodeURIComponent(name)}`).then((d) => d.bibtex ?? ""),
      `copied BibTeX for #${name}`
    );
  } else if (action === "append-bib") {
    say(`appending #${name} to its .bib …`, 0);
    const res = await post("/api/collections/append-bib", { name, work_ids: [] });
    if (res.detail) { say(res.detail, 9000); return; }
    const n = res.added?.length ?? 0;
    const p = res.present?.length ?? 0;
    say(n ? `appended ${n} new ${n === 1 ? "entry" : "entries"} to ${res.bib_file}${p ? ` (${p} already there)` : ""}`
          : `all ${p} papers are already in ${res.bib_file}`, 9000);
  } else if (action === "folder") {
    const res = await post("/api/collections/open", { name, what: "folder" });
    if (res.detail) say(res.detail, 8000);
  } else if (action === "proxy5") {
    const stubs = (state.collectionAll ?? state.raw ?? []).filter((i) => !i.locations?.length);
    let opened = 0;
    for (const item of stubs.slice(0, 5)) {
      const workData = await get(`/api/work/${item.work_id}`);
      const page = workData.work?.user?.source_url || (workData.work?.meta?.doi ? `https://doi.org/${workData.work.meta.doi}` : "");
      if (page) {
        window.open(proxyUrl(state.proxy, page), "_blank", "noopener");
        opened++;
      }
    }
    say(opened ? `opened ${opened} papers through your library proxy` : "none of these stubs have a DOI or URL yet", 7000);
  }
}
