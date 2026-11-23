// Resilient 3-stage batch import (.bib, DOI/URL lists, or plain-text syllabi).

import { $, esc, post, say } from "./api.js";

const imp = {
  text: "",
  collection: "",
  queue: false,
  useLlm: false,
  downloadOa: true,
  preview: null,
  picked: new Set(),
  busy: false,
};

export function openImportView(prefillCollection = "", ctx) {
  if (prefillCollection) imp.collection = prefillCollection;
  ctx.state.collection = null;
  ctx.state.context = null;
  ctx.state.tag = null;
  ctx.state.view = "import";
  ctx.markSidebar();
  ctx.search();
}

export async function showImport(token, ctx) {
  if (token !== ctx.listToken()) return;
  ctx.state.items = [];
  $("count").textContent = "batch import";
  drawImport(ctx);
}

const STATUS_LABEL = {
  existing: ["■ already in refs", "ok"],
  new_oa: ["+ new (open PDF)", "accent"],
  new_stub: ["□ new (stub, no OA PDF)", "warn"],
  unresolved: ["! unresolved", "bad"],
};

function drawImport(ctx) {
  const prev = imp.preview;
  let html = `<div class="impwrap">
    <div class="label" style="margin-top:8px">Batch import (.bib · DOIs/URLs · syllabus)</div>
    <textarea id="imp-text" class="note imp-area" spellcheck="false"
      placeholder="Paste a .bib file, one DOI/URL/arXiv/NBER id per line, or a syllabus reading list (drop a .bib file anywhere in this box)…">${esc(imp.text)}</textarea>
    <div class="dlopts" style="margin-top:8px">
      <label>into #<input id="imp-coll" class="imp-inline" value="${esc(imp.collection)}" placeholder="e.g. 220a or bets" spellcheck="false"></label>
      <label><input type="checkbox" id="imp-queue"${imp.queue ? " checked" : ""}> add to queue</label>
      <label><input type="checkbox" id="imp-llm"${imp.useLlm ? " checked" : ""}> parse plain-text syllabus with Claude</label>
      <label><input type="checkbox" id="imp-oa"${imp.downloadOa ? " checked" : ""}> download open-access PDFs</label>
    </div>
    <div class="acts" style="margin-top:10px">
      <button class="act" data-imp="preview"${imp.busy ? " disabled" : ""}>${imp.busy ? "resolving …" : "preview & match"}</button>
      <label class="act" style="cursor:pointer">load .bib file…<input type="file" id="imp-file" accept=".bib,.txt,.md" hidden></label>
      ${prev ? `<button class="act" data-imp="clear">clear preview</button>` : ""}
    </div>`;

  if (prev) {
    const c = prev.counts ?? {};
    html += `<div class="bar" style="margin-top:12px">
      <button class="act" data-imp="commit"${imp.picked.size && !imp.busy ? "" : " disabled"}>import ${imp.picked.size} selected</button>
      <button class="act" data-imp="all">select valid</button>
      <button class="act" data-imp="none">none</button>
      <span class="badge">${c.existing || 0} in refs · ${c.new_oa || 0} open PDF · ${c.new_stub || 0} stubs · ${c.unresolved || 0} unresolved</span>
    </div>`;
    if (!prev.items.length) {
      html += `<div class="empty">Nothing parseable found in that text. Try ticking "parse plain-text syllabus with Claude" for free-form syllabi.</div>`;
    } else {
      for (const item of prev.items) {
        const [badge, cls] = STATUS_LABEL[item.status] ?? [item.status, ""];
        const disabled = item.status === "unresolved";
        const checked = !disabled && imp.picked.has(item.idx);
        const who = item.authors?.length ? `${item.authors.slice(0, 3).join(", ")}${item.authors.length > 3 ? " et al." : ""}` : "";
        const sub = [who, item.year, item.venue, item.topic ? `→ /${item.topic}` : ""].filter(Boolean).join(" · ");
        html += `<label class="prop dl${disabled ? " off" : ""}">
          <input type="checkbox" data-imp-idx="${item.idx}"${checked ? " checked" : ""}${disabled ? " disabled" : ""}>
          <span class="name">${esc(item.title || "(untitled)")}</span>
          <span class="badge imp-${cls}">${esc(badge)}</span>
          <span class="who">${esc(sub || item.source_url || "")}</span>
        </label>`;
      }
    }
  }
  html += `</div>`;
  $("list").innerHTML = html;

  $("detail").innerHTML = `<div class="label">How resilient import works</div>
    <p>1. <b>Preview & match</b> checks your library first so existing papers are simply tagged into your collection—zero duplicates.</p>
    <p class="where" style="margin-top:8px">2. New papers are added immediately with verified metadata. Only reliable open-access PDFs (arXiv, NBER, OpenAlex OA, direct PDF links) download in the background.</p>
    <p class="where" style="margin-top:8px">3. Paywalled papers become clean <b>no pdf</b> stubs in the collection—open that filter afterwards and use <b>open next 5 via proxy</b> + your bookmarklet to grab them reliably through your browser.</p>`;
}

export function wireImportEvents(ctx) {
  $("list").addEventListener("input", (event) => {
    if (ctx.state.view !== "import") return;
    if (event.target.id === "imp-text") imp.text = event.target.value;
    if (event.target.id === "imp-coll") imp.collection = event.target.value.trim().replace(/^#/, "");
  });
  $("list").addEventListener("change", async (event) => {
    if (ctx.state.view !== "import") return;
    const el = event.target;
    if (el.id === "imp-queue") imp.queue = el.checked;
    else if (el.id === "imp-llm") imp.useLlm = el.checked;
    else if (el.id === "imp-oa") imp.downloadOa = el.checked;
    else if (el.id === "imp-file" && el.files?.[0]) {
      imp.text = await el.files[0].text();
      drawImport(ctx);
    } else if (el.dataset.impIdx !== undefined) {
      const idx = Number(el.dataset.impIdx);
      el.checked ? imp.picked.add(idx) : imp.picked.delete(idx);
      const btn = $("list").querySelector('[data-imp="commit"]');
      if (btn) { btn.disabled = !imp.picked.size; btn.textContent = `import ${imp.picked.size} selected`; }
    }
  });
  $("list").addEventListener("click", async (event) => {
    if (ctx.state.view !== "import") return;
    const btn = event.target.closest("[data-imp]");
    if (!btn) return;
    const act = btn.dataset.imp;
    if (act === "clear") { imp.preview = null; imp.picked.clear(); drawImport(ctx); return; }
    if (act === "all" && imp.preview) {
      imp.picked = new Set(imp.preview.items.filter((i) => i.status !== "unresolved").map((i) => i.idx));
      drawImport(ctx); return;
    }
    if (act === "none") { imp.picked.clear(); drawImport(ctx); return; }
    if (act === "preview") {
      if (!imp.text.trim()) { say("paste a .bib, DOIs/URLs, or a reading list first"); return; }
      imp.busy = true; drawImport(ctx);
      say("reconciling against your library and registries …", 0);
      try {
        const res = await post("/api/import/preview", { text: imp.text, use_llm: imp.useLlm, resolve_online: true });
        imp.preview = res;
        imp.picked = new Set((res.items ?? []).filter((i) => i.status !== "unresolved").map((i) => i.idx));
        say(`found ${res.items?.length ?? 0} entries`);
      } catch (e) {
        say(`preview failed: ${e}`, 8000);
      }
      imp.busy = false; drawImport(ctx);
      return;
    }
    if (act === "commit" && imp.preview) {
      const chosen = imp.preview.items.filter((i) => imp.picked.has(i.idx));
      if (!chosen.length) return;
      imp.busy = true; drawImport(ctx);
      const res = await post("/api/import/commit", {
        items: chosen,
        collection_name: imp.collection || null,
        queue: imp.queue,
        download_oa: imp.downloadOa,
      });
      imp.busy = false;
      if (res.detail) { say(res.detail, 9000); drawImport(ctx); return; }
      const targetCol = imp.collection;
      imp.preview = null; imp.picked.clear(); imp.text = "";
      say(`imported: ${res.tagged} existing tagged, ${res.created} new works added` +
          (res.oa_queued ? ` (downloading ${res.oa_queued} open PDFs …)` : ""), 10000);
      await ctx.loadSidebar();
      if (targetCol) {
        ctx.state.collection = targetCol;
        ctx.state.collectionFilter = "all";
        ctx.state.view = "all";
        ctx.markSidebar();
        ctx.search();
      } else {
        drawImport(ctx);
      }
      if (res.job) ctx.follow(Promise.resolve({ job: res.job }), "open-access PDFs");
    }
  });
}
