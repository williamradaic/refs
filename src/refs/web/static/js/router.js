// Lightweight URL hash router: Back/Forward, reload persistence, deep links (#w/<id>, #c/<name>).

export function encodeHash(state, selectedId = null) {
  const parts = [];
  if (state.collection) {
    parts.push(`c=${encodeURIComponent(state.collection)}`);
    if (state.collectionFilter && state.collectionFilter !== "all") {
      parts.push(`f=${encodeURIComponent(state.collectionFilter)}`);
    }
  } else if (state.tag) {
    parts.push(`t=${encodeURIComponent(state.tag)}`);
  } else if (state.context) {
    parts.push(`x=${encodeURIComponent(state.context)}`);
  } else if (state.view && state.view !== "all") {
    parts.push(`v=${encodeURIComponent(state.view)}`);
  }
  if (state.query) parts.push(`q=${encodeURIComponent(state.query)}`);
  if (selectedId) parts.push(`w=${encodeURIComponent(selectedId)}`);
  return parts.length ? `#${parts.join("&")}` : "";
}

export function decodeHash(hash = location.hash) {
  const raw = String(hash ?? "").replace(/^#/, "");
  if (!raw) return null;
  const params = new URLSearchParams(raw);
  return {
    collection: params.get("c") || null,
    collectionFilter: params.get("f") || "all",
    tag: params.get("t") || null,
    context: params.get("x") || null,
    view: params.get("v") || "all",
    query: params.get("q") || "",
    workId: params.get("w") || null,
  };
}

let lastHash = "";

export function pushHash(state, selectedId = null, replace = false) {
  const next = encodeHash(state, selectedId);
  if (next === lastHash || next === location.hash) return;
  lastHash = next;
  const url = next || location.pathname + location.search;
  try {
    if (replace) history.replaceState(null, "", url);
    else history.pushState(null, "", url);
  } catch {}
}

export function workLink(workId, collection = null) {
  const base = `${location.origin}${location.pathname}`;
  const parts = [];
  if (collection) parts.push(`c=${encodeURIComponent(collection)}`);
  parts.push(`w=${encodeURIComponent(workId)}`);
  return `${base}#${parts.join("&")}`;
}
