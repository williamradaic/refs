// Shared DOM, HTTP, formatting and clipboard helpers for refs.

export const $ = (id) => document.getElementById(id);

export const esc = (text) => String(text ?? "").replace(/[&<>"]/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

const SEASONS = { winter: 0, spring: 1, summer: 2, fall: 3 };
export const termRank = (term) => {
  const [year, season] = String(term ?? "").split("-");
  return term ? Number(year) * 4 + (SEASONS[season] ?? 0) : Infinity;
};

export const clock = (t) => new Date(t * 1000).toLocaleTimeString([], {
  hour: "2-digit", minute: "2-digit", hourCycle: "h23",
});

export const shortDay = (iso) => new Date(iso).toLocaleDateString([], {
  day: "numeric", month: "short",
});

export function proxyUrl(proxy, url) {
  if (!proxy || !url) return url || "";
  return proxy + (/qurl=$/i.test(proxy) ? encodeURIComponent(url) : url);
}

export async function get(url, signal = undefined) {
  return (await fetch(url, signal ? { signal } : undefined)).json();
}

export async function post(url, body) {
  // The X-Refs header is what the server checks before changing anything.
  const response = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Refs": "1" },
    body: JSON.stringify(body ?? {}),
  });
  return response.json();
}

let sayTimer = null;
export function say(text, ms = 4000) {
  const el = $("message");
  if (!el) return;
  el.textContent = text;
  clearTimeout(sayTimer);
  if (ms) sayTimer = setTimeout(() => (el.textContent = ""), ms);
}

export async function copyText(text, message) {
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

export function copyBibtex(ids) {
  const text = Promise.all(ids.map((id) => get(`/api/work/${id}`).then((d) => d.bibtex ?? "", () => "")))
    .then((entries) => entries.filter(Boolean).join("\n"));
  copyText(text, `copied BibTeX for ${ids.length === 1 ? "1 paper" : `${ids.length} papers`}`);
}
