// Settings view (extracted from app.js): edits ~/.config/refs/config.toml via /api/settings.

import { $, esc, get, post, say } from "./api.js";

const settings = { data: null, draft: {}, error: null, authors: null };
const LIST_KINDS = new Set(["folders", "names", "globs", "rules", "authors"]);

const sValue = (f) => (f.key in settings.draft ? settings.draft[f.key] : f.value);
const sDirty = () => Object.keys(settings.draft).length > 0;

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

export async function showSettings(token, ctx) {
  const data = await get("/api/settings");
  if (token !== ctx.listToken()) return;
  ctx.state.items = [];
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

async function saveSettings(ctx) {
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
  setTimeout(ctx.loadSidebar, 1500);
}

function sAddTyped(input) {
  const key = input.dataset.sAdd;
  const text = input.value.trim();
  if (!text) return;
  const field = settings.data.fields.find((f) => f.key === key);
  input.value = "";
  sSet(key, [...sValue(field).filter((v) => v !== text), text]);
}

export function wireSettingsEvents(ctx) {
  $("list").addEventListener("click", async (event) => {
    const el = event.target.closest("button");
    if (!el || ctx.state.view !== "settings") return;
    const d = el.dataset;
    if (d.sSave) await saveSettings(ctx);
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
    if (ctx.state.view !== "settings") return;
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
}
