// Detail pane enhancements: 4-state reading strip + reading log, queue intent + up-next,
// personal & model tags, PDF highlights panel, Obsidian backlinks, merge & remove.

import { $, esc, get, post, say } from "./api.js";

export const READ_ORDER = ["unread", "reading", "skimmed", "read"];
const READ_GLYPHS = { unread: "○", reading: "◐", skimmed: "◔", read: "●" };

export function readingStripHtml(item, work) {
  if (!work) return "";
  const cur = item.read_status ?? "unread";
  const pills = READ_ORDER.map((st) =>
    `<button class="pill${cur === st ? " on" : ""}" data-setread="${st}" title="set status: ${st}">${READ_GLYPHS[st]} ${st}</button>`
  ).join("");
  const auto = work.read_auto;
  const detected = item.read_source === "detected" && auto
    ? ` <span class="badge">· markup on ${auto.pages}/${auto.of}p</span>` : "";

  const reads = [...(work.user?.reads ?? [])].reverse();
  const history = reads.length
    ? `<div class="readlog">${reads.slice(0, 6).map((r) => {
        const ctx = r.context ? ` <span class="tag" data-coll-show="${esc(r.context)}">#${esc(r.context)}</span>` : "";
        const note = r.note ? ` — ${esc(r.note)}` : "";
        return `<div><span class="badge">${esc(r.at)}</span> ${esc(r.status)}${ctx}${note}</div>`;
      }).join("")}</div>` : "";

  const queued = Boolean(item.queued);
  const isNext = item.queue_priority === "next";
  const qNote = item.queue_note ?? "";
  const qFor = item.queue_for ? `<span class="badge">#${esc(item.queue_for)}</span>` : "";
  const queueBox = queued
    ? `<div class="qbar">
        <button class="act" data-act="queue" title="take off queue (q)">▸ queued</button>
        <button class="act${isNext ? " qpin-on" : ""}" data-qpin="1" title="pin to 'up next' at the top of the queue">${isNext ? "★ up next" : "☆ up next"}</button>
        ${qFor}
        <input id="qnote" class="qnote" value="${esc(qNote)}" placeholder="why did I queue this? (Enter to save)" autocomplete="off">
      </div>`
    : `<div class="qbar">
        <button class="act" data-act="queue" title="put on reading queue (q; Q to add a reason)">+ queue</button>
        <button class="act" data-reread-open="1">+ log read</button>
      </div>`;

  return `<div class="rdstrip">
    <div class="rdpills">${pills}${detected}${queued ? ` <button class="act" data-reread-open="1" style="margin-left:auto">+ log read</button>` : ""}</div>
    ${queueBox}
    <div id="reread-form"></div>
    ${history}
  </div>`;
}

export function tagsSectionHtml(work, item) {
  const userTags = work?.user?.tags ?? item.user_tags ?? [];
  const autoTags = work ? (work.tags ?? []) : (item.tags ?? []);
  const hiddenCount = work?.user?.hidden_tags?.length ?? 0;

  let html = `<div class="label">Tags</div><div class="chips" style="margin-bottom:4px">`;
  for (const t of userTags) {
    html += `<span class="chip utag"><span class="tag" data-tag="${esc(t)}">${esc(t)}</span>` +
      `<button class="act" data-utag-del="${esc(t)}" title="remove personal tag">×</button></span>`;
  }
  for (const t of autoTags) {
    html += `<span class="chip atag"><span class="tag" data-tag="${esc(t)}">${esc(t)}</span>` +
      (work ? `<button class="act" data-atag-hide="${esc(t)}" title="hide auto tag">×</button>` : "") +
      `</span>`;
  }
  if (!userTags.length && !autoTags.length) html += `<span class="where">none</span>`;
  html += `</div>`;
  if (work) {
    html += `<div class="adder" style="margin-top:2px">
      <input id="utag-in" placeholder="+ personal tag (Enter)" autocomplete="off" spellcheck="false" style="flex:0 1 24ch">
      ${hiddenCount ? `<button class="act" data-atag-restore="1">restore ${hiddenCount} hidden</button>` : ""}
    </div>`;
  }
  return html;
}

export function backlinksHtml(backlinks) {
  if (!backlinks?.length) return "";
  return `<div class="backlinks"><span class="badge">referenced in:</span> ` +
    backlinks.map((b) => `<a class="tag" href="${esc(b.uri)}" title="${esc(b.file)}">[[${esc(b.title)}]]</a>`).join(" · ") +
    `</div>`;
}

export function highlightsHtml(work, items, noteAvailable) {
  if (!items?.length) return "";
  const rows = items.map((h) => {
    const dot = h.color ? `<span class="hldot" style="background:${esc(h.color)}"></span>` : "";
    const quote = h.quote ? `<span class="hlq">“${esc(h.quote)}”</span>` : "";
    const comment = h.comment ? `<div class="hlc">${esc(h.comment)}</div>` : "";
    return `<div class="hlrow"><span class="hlp">p.${h.page}</span>${dot}<div class="hlbody">${quote}${comment}</div></div>`;
  }).join("");
  return `<div class="label">Highlights <span class="badge">(${items.length})</span>
      ${noteAvailable ? `<button class="act" data-hl-pull="1" style="margin-left:auto;text-transform:none;letter-spacing:0">pull into note</button>` : ""}
    </div>
    <details class="hlbox"${items.length <= 5 ? " open" : ""}>
      <summary class="where">show ${items.length} ${items.length === 1 ? "highlight" : "highlights"} &amp; comments</summary>
      <div class="hllist">${rows}</div>
    </details>`;
}

export function showRereadForm(defaultContext = "") {
  const box = $("reread-form");
  if (!box) return;
  if (box.innerHTML) { box.innerHTML = ""; return; }
  const today = new Date().toISOString().slice(0, 10);
  box.innerHTML = `<form class="edit" id="rereadform">
    <label><span>date</span><input name="at" type="date" value="${today}"></label>
    <label><span>depth</span><select name="status">
      <option value="read" selected>read</option>
      <option value="skimmed">skimmed</option>
      <option value="reading">reading</option>
    </select></label>
    <label><span>for</span><input name="context" value="${esc(defaultContext)}" placeholder="project or course (optional)" spellcheck="false"></label>
    <label><span>note</span><input name="note" placeholder="1-line takeaway (optional)"></label>
    <div class="acts"><button class="act" type="submit">log</button>
      <button class="act" type="button" data-reread-cancel="1">cancel</button></div>
  </form>`;
  box.querySelector('input[name="note"]')?.focus();
}

let mergeTimer = null;
export function openMergePicker(workId) {
  const box = $("mergepicker");
  if (!box) return;
  if (box.innerHTML) { box.innerHTML = ""; return; }
  box.innerHTML = `<div class="picker" style="margin-top:6px">
    <div class="where">Merge another duplicate work INTO this one (files, collections, notes combine; this citekey stays):</div>
    <input class="pick" id="merge-q" placeholder="search the duplicate paper to absorb…" autocomplete="off" spellcheck="false">
    <div id="merge-hits"></div>
  </div>`;
  const input = $("merge-q");
  input.focus();
  input.addEventListener("input", () => {
    clearTimeout(mergeTimer);
    mergeTimer = setTimeout(async () => {
      const q = input.value.trim();
      if (!q) { $("merge-hits").innerHTML = ""; return; }
      const data = await get(`/api/search?q=${encodeURIComponent(q)}&view=all&scope=library&ask=off`);
      const hits = (data.items ?? []).filter((i) => i.work_id && i.work_id !== workId).slice(0, 8);
      $("merge-hits").innerHTML = hits.length
        ? hits.map((h) => `<div class="opt" data-merge-drop="${esc(h.work_id)}">
            <span>${esc(h.title || h.citekey)}</span>
            <span class="badge">${esc((h.authors?.[0] ?? "") + " " + (h.year ?? ""))} · @${esc(h.citekey)}</span>
          </div>`).join("")
        : `<div class="where">no matching works</div>`;
    }, 150);
  });
}

export async function saveQueueNote(workId, note) {
  const res = await post(`/api/work/${workId}/queue`, { on: null, note });
  if (res.detail) { say(res.detail, 8000); return null; }
  say(note.trim() ? "saved queue note" : "cleared queue note");
  return res;
}
