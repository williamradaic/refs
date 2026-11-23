// First-run onboarding wizard (extracted from app.js).

import { $, esc, get, post, say } from "./api.js";

const ob = {
  step: 0, info: null, library: null, survey: null, open: new Set(),
  roots: new Set(), dump: "papers-dump", exclude: new Set(), noLlm: new Set(),
  vault: "", vaultFolder: "refs", read: "dump", saving: false, startedPass: null,
};
const OB_STEPS = ["welcome", "library", "papers", "dump", "private", "notes", "reading", "progress"];
let obStatus = null;

const obUnder = (path, folders) => [...folders].some((f) => path === f || path.startsWith(f + "/"));
function obPdfs() {
  let n = 0;
  const walk = (node) => {
    if (ob.roots.has(node.path) && !obUnder(node.path, ob.exclude)) { n += node.pdfs; return; }
    node.children.forEach(walk);
  };
  if (ob.survey) walk(ob.survey.tree);
  return n;
}
const obRules = () => (ob.survey?.term_rules ?? []).filter((r) => obUnder(r.split("/{term}")[0], ob.roots));
const dollars = (x) => (x < 0.01 ? "under $0.01" : `about $${x < 10 ? x.toFixed(2) : Math.round(x)}`);

function obTree(node, depth) {
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
  } else if (name === "library") {
    const options = [...info.libraries];
    if (!options.some((o) => o.path === ob.library)) options.push({ label: "chosen", path: ob.library });
    body = `<h1>Where do you keep your papers?</h1>
      <p class="where">The library folder: refs looks for papers inside it, and nowhere else.
      Usually your Dropbox or iCloud Drive.</p>
      <div class="ob-choices">${options.map((o) => `<label><input type="radio" name="ob-lib" value="${esc(o.path)}"
        ${o.path === ob.library ? " checked" : ""}> ${esc(o.label)} <span class="badge">${esc(o.path)}</span></label>`).join("")}</div>
      <button class="act" data-ob-choose="library">choose another folder…</button>
      <div class="ob-nav">${back}${next()}</div>`;
  } else if (name === "papers") {
    if (!ob.survey) body = `<h1>Counting PDFs …</h1><p class="where">in ${esc(ob.library)}</p>`;
    else if (ob.survey.error) body = `<h1>That folder cannot be read</h1><p class="bad">${esc(ob.survey.error)}</p><div class="ob-nav">${back}</div>`;
    else {
      const n = obPdfs();
      body = `<h1>Which folders hold papers?</h1>
        <p class="where">Tick the folders refs should watch; everything inside them is included.
        Numbers are PDFs. ▸ opens a folder, so you can pick only part of it.</p>
        ${ob.survey.complete ? "" : `<p class="bad">This library is large: counting stopped after ${ob.survey.seconds} s, so the numbers are low.</p>`}
        <div class="ob-tree">${obTree(ob.survey.tree, 0)}</div>
        <p class="ob-sum">${ob.roots.size ? `${n.toLocaleString()} PDFs in ${ob.roots.size} folder${ob.roots.size > 1 ? "s" : ""}` : "nothing chosen yet"}</p>
        ${obRules().map((r) => `<p class="where">Term folders in <b>${esc(r.split("/{term}")[0])}</b>: the folders inside each term are courses, grouped by term.</p>`).join("")}
        <div class="ob-nav">${back}${next("next", !ob.roots.size)}</div>`;
    }
  } else if (name === "dump") {
    body = `<h1>Where should new papers go?</h1>
      <p class="where">The quick-add folder. Papers you add in refs are saved here, and refs gives them tidy names. Created if it does not exist, inside the library folder.</p>
      <div class="adder"><input id="ob-dump" value="${esc(ob.dump)}" spellcheck="false" autocomplete="off"></div>
      <div class="ob-nav">${back}${next()}</div>`;
  } else if (name === "private") {
    const shown = (ob.survey?.suggestions ?? []).filter((s) => obUnder(s.path, ob.roots));
    const row = (path, kind) => {
      const set = kind === "no_llm" ? ob.noLlm : ob.exclude;
      return `<label><input type="checkbox" data-ob-private="${esc(path)}" data-kind="${kind}"${set.has(path) ? " checked" : ""}>
        ${esc(path)} <span class="badge">· ${kind === "no_llm" ? "index, but never send to Claude" : "leave out entirely"}</span></label>`;
    };
    const chosen = [...ob.exclude].map((p) => [p, "exclude"]).concat([...ob.noLlm].map((p) => [p, "no_llm"]));
    const listed = new Set(shown.map((s) => s.path));
    body = `<h1>Anything private in there?</h1>
      <p class="where">Paperwork (visa, taxes, insurance) can be <b>left out entirely</b>. Confidential work (a manuscript you referee) can be indexed but <b>never sent to Claude</b>.</p>
      ${shown.length ? `<p>Suggested from folder names:</p>` : `<p class="where">No folder names suggest paperwork.</p>`}
      <div class="ob-choices">${shown.map((s) => row(s.path, s.kind)).join("")}
        ${chosen.filter(([p]) => !listed.has(p)).map(([p, k]) => row(p, k)).join("")}</div>
      <div class="acts"><button class="act" data-ob-choose="exclude">leave out a folder…</button>
        <button class="act" data-ob-choose="no_llm">keep a folder from Claude…</button></div>
      <div class="ob-nav">${back}${next()}</div>`;
  } else if (name === "notes") {
    const vaults = [...new Set([...info.vaults, ob.vault].filter(Boolean))];
    body = `<h1>Reading notes in Obsidian?</h1>
      <p class="where">refs can keep a note for each paper as a Markdown file in an Obsidian vault, so <code>[[citekey]]</code> links to them.</p>
      <div class="ob-choices">${vaults.map((v) => `<label><input type="radio" name="ob-vault" value="${esc(v)}"
        ${v === ob.vault ? " checked" : ""}> ${esc(v)}</label>`).join("")}
        <label><input type="radio" name="ob-vault" value=""${ob.vault ? "" : " checked"}> no notes for now</label></div>
      <button class="act" data-ob-choose="vault">choose a vault…</button>
      ${ob.vault ? `<p style="margin-top:10px">Folder in the vault:</p>
        <div class="adder"><input id="ob-vault-folder" value="${esc(ob.vaultFolder)}" spellcheck="false"></div>` : ""}
      <div class="ob-nav">${back}${next()}</div>`;
  } else if (name === "reading") {
    const n = obPdfs();
    const cost = n * info.cost_per_pdf;
    const minutes = Math.max(1, Math.round(n * 4 / 60));
    const key = { set: "found in the macOS Keychain", environment: "from ANTHROPIC_API_KEY" }[info.api_key];
    const option = (value, label, note) => `<label><input type="radio" name="ob-read" value="${value}"
      ${ob.read === value ? " checked" : ""}> ${label} <span class="badge">· ${note}</span></label>`;
    body = `<h1>Reading your papers</h1>
      <p>To tell what each PDF is, refs sends the <b>text of its first three pages</b> (an image of page one for scans) to Claude Haiku, then checks Crossref and OpenAlex (~$0.001/PDF).</p>
      <div class="label">API key</div>
      ${key ? `<p class="where">${esc(key)} ✓</p>` : `<p class="where">Create one at
        <a href="https://console.anthropic.com/settings/keys" target="_blank" rel="noreferrer">console.anthropic.com</a>,
        then paste it here (stored in the macOS Keychain).</p>
        <div class="adder"><input id="ob-key" type="password" placeholder="sk-ant-…" autocomplete="off" spellcheck="false">
        <button class="act" data-ob-key="1">check and save</button></div>`}
      <div class="label">What to read</div>
      <div class="ob-choices">
        ${option("all", `everything: ${n.toLocaleString()} PDFs now`, `${dollars(cost)}, ${minutes} min or so, then each new PDF`)}
        ${option("dump", "only papers I add from now on", "the others wait under “not identified”, free")}
        ${option("none", "nothing for now", "read later with `refs process`")}
      </div>
      ${ob.read === "all" && cost > info.read_cap ? `<p class="where">Above the monthly limit ($${info.read_cap}): raise it under settings.</p>` : ""}
      ${info.api_key === "missing" && ob.read !== "none" ? `<p class="bad">Without a key nothing can be read; PDFs are still indexed by file name.</p>` : ""}
      <div class="ob-nav">${back}${next(ob.saving ? "saving …" : "finish setup", ob.saving)}</div>
      ${ob.error ? `<p class="bad">${esc(ob.error)}</p>` : ""}`;
  } else if (name === "progress") {
    body = obProgressHtml();
  }
  box.innerHTML = `<div class="ob-page"><div class="ob-dots">${dots}</div>${body}</div>`;
}

function obProgressHtml() {
  const s = obStatus;
  const p = s?.progress;
  const finished = s && !p && !s.pending && s.last_pass && s.last_pass !== ob.startedPass;
  if (!ob.info.watching) {
    return `<h1>Saved</h1><p class="where">Running without the folder watcher (--no-watch). Run <code>refs process --all</code> to read.</p>
      <div class="ob-nav"><button class="act primary" data-ob-done="1">open my library</button></div>`;
  }
  if (!finished) {
    const what = !p ? "starting …"
      : p.stage === "scanning" ? `scanning: ${p.done.toLocaleString()} of ${p.total.toLocaleString()} PDFs`
      : p.stage === "indexing" ? "building the search index (downloading 70 MB search model first time)"
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
                  none: "Run <code>refs service install</code> in Terminal to start at login." }[ob.info.service];
  return `<h1>Your library is ready</h1>
    <p>${esc(s.message || "Indexed.")}</p>
    <div class="label">Next</div>
    <ul class="ob-list">
      <li>Search with <kbd>/</kbd>, open with <kbd>⏎</kbd>, all keys with <kbd>?</kbd>.</li>
      <li>Add papers with the <a href="/bookmarklet" target="_blank">bookmarklet</a>, or with <kbd>a</kbd>.</li>
      <li>${login}</li>
    </ul>
    <div class="ob-nav"><button class="act primary" data-ob-done="1">open my library</button></div>`;
}

async function obFollow() {
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
  const response = await fetch("/api/settings", { method: "POST",
    headers: { "Content-Type": "application/json", "X-Refs": "1" },
    body: JSON.stringify({ changes, approve_reads: ob.read === "all" }) });
  const answer = await response.json();
  ob.saving = false;
  if (!response.ok) { ob.error = answer.error; return drawOnboarding(); }
  ob.step++;
  obFollow();
}

export async function maybeOnboard(ctx) {
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

  document.addEventListener("click", async (event) => {
    const cur = $("onboard");
    const el = event.target.closest("button");
    if (!cur || !el || !cur.contains(el)) return;
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
    } else if (d.obKey) {
      const input = $("ob-key");
      say("checking the key with Anthropic …", 0);
      const response = await fetch("/api/settings/key", { method: "POST",
        headers: { "Content-Type": "application/json", "X-Refs": "1" }, body: JSON.stringify({ key: input.value }) });
      const answer = await response.json();
      if (!response.ok) { say(answer.error, 8000); return; }
      say("key saved in the Keychain");
      ob.info.api_key = answer.api_key;
      if (!ob.readChosen) ob.read = obPdfs() * ob.info.cost_per_pdf <= 5 ? "all" : "dump";
      drawOnboarding();
    } else if (d.obDone) {
      cur.remove();
      ctx.loadSidebar(); ctx.search();
    }
  });

  document.addEventListener("change", (event) => {
    const cur = $("onboard");
    const el = event.target;
    if (!cur || !cur.contains(el)) return;
    if (el.dataset.obRoot !== undefined) {
      const path = el.dataset.obRoot;
      if (el.checked) {
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
}
