// Review -> published: check working papers against Crossref & OpenAlex for published upgrades.

import { $, esc, get, post, say, proxyUrl } from "./api.js";

let latestData = null;

export async function showUpdates(token, ctx) {
  const data = await get("/api/updates");
  if (token !== ctx.listToken()) return;
  latestData = data;
  ctx.state.items = [];
  $("count").textContent = `${data.items.length} published updates`;
  renderUpdates(data, ctx);
}

function renderUpdates(data, ctx) {
  const checked = data.checked_at ? `last checked ${data.checked_at.slice(0, 10)}` : "never checked yet";
  let html = `<div class="bar">
    <button class="act" data-upd-check="1">check ${data.candidates} working papers now</button>
    <span class="badge">${esc(checked)}</span>
  </div>`;

  if (!data.items.length) {
    html += `<div class="empty">No published versions waiting for review.${
      data.candidates ? ` Click <b>check ${data.candidates} working papers now</b> to query Crossref and OpenAlex.` : ""
    }</div>`;
  } else {
    for (const item of data.items) {
      const cur = item.current ?? {};
      const prop = item.proposed ?? {};
      const issue = [prop.volume, prop.issue && `(${prop.issue})`].filter(Boolean).join("");
      const pubWhere = [prop.venue, prop.year, issue, prop.pages].filter(Boolean).join(" · ");
      const via = item.open_url && ctx.state.proxy ? proxyUrl(ctx.state.proxy, item.open_url) : item.open_url;
      html += `<div class="copyset upditem" data-upd-work="${esc(item.work_id)}">
        <div class="topic">
          <span>${esc(item.title)}</span>
          <span class="badge">@${esc(item.citekey)} · ${esc(item.similarity)}%</span>
        </div>
        <div class="updgrid">
          <span class="badge">now</span>
          <span class="where">${esc([cur.venue || "working paper", cur.year, cur.doi].filter(Boolean).join(" · "))}</span>
          <span class="badge">found</span>
          <span class="updpub">${esc(pubWhere)}${prop.doi ? ` · <a href="https://doi.org/${esc(prop.doi)}" target="_blank" rel="noreferrer">${esc(prop.doi)}</a>` : ""}</span>
        </div>
        <div class="acts" style="padding:2px 2ch 8px 4ch">
          <button class="act" data-upd-apply="${esc(item.work_id)}">accept upgrade</button>
          <button class="act" data-upd-ignore="${esc(item.work_id)}" data-upd-key="${esc(item.key)}">ignore</button>
          ${via ? `<a class="act" href="${esc(via)}" target="_blank" rel="noreferrer">${ctx.state.proxy ? "journal via proxy" : "open journal"}</a>` : ""}
        </div>
      </div>`;
    }
  }
  $("list").innerHTML = html;
  $("detail").innerHTML = `<div class="label">Published updates</div>
    <p>Re-checks your working papers, drafts, NBER/arXiv/SSRN postings against Crossref and OpenAlex (bypassing old cached misses).</p>
    <p class="where" style="margin-top:8px"><b>accept upgrade</b> updates venue, year, volume, pages and DOI while keeping your citekey, collections, reading log, notes and PDF intact (the original working-paper info stays saved on the record).</p>`;
}

export async function handleUpdatesClick(el, ctx) {
  if (el.dataset.updCheck) {
    say("checking working papers against Crossref & OpenAlex …", 0);
    await ctx.follow(post("/api/updates/check", {}), "published updates");
    return true;
  }
  if (el.dataset.updApply) {
    const workId = el.dataset.updApply;
    const item = latestData?.items?.find((i) => i.work_id === workId);
    const res = await post("/api/updates/apply", { work_id: workId, proposed: item?.proposed ?? null });
    if (res.detail) { say(res.detail, 9000); return true; }
    say(`upgraded @${res.work.citekey} to ${res.work.meta.venue} (${res.work.meta.year})`, 8000);
    ctx.loadSidebar();
    ctx.search();
    return true;
  }
  if (el.dataset.updIgnore) {
    await post("/api/updates/ignore", { work_id: el.dataset.updIgnore, key: el.dataset.updKey });
    say("dismissed update");
    ctx.loadSidebar();
    ctx.search();
    return true;
  }
  return false;
}
