"use strict";

const $ = (id) => document.getElementById(id);

const TIER_LABELS = {
  exact: "tier 1 · exact",
  sourced: "tier 2 · sourced",
  attributed: "tier 3 · attributed",
  orphan: "tier 4 · orphan",
};

async function api(path, options) {
  const res = await fetch(path, options);
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(typeof body.detail === "string" ? body.detail : res.statusText);
  }
  return res.json();
}

function postJson(path, body, method) {
  return api(path, {
    method: method || "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`);
}

// Predicates are ALL_CAPS identifiers; mirror the server's normalization so
// the user sees what will actually be stored.
function normalizePredicate(s) {
  return s.trim().replace(/[\s-]+/g, "_").toUpperCase();
}

function flash(message, ok) {
  const el = $("flash");
  el.textContent = message;
  el.className = `flash ${ok ? "ok" : "err"}`;
  if (ok) setTimeout(() => (el.textContent = ""), 2500);
}

async function loadStats() {
  try {
    const s = await api("/api/stats");
    $("stats").textContent =
      `${s.artifact} artifacts · ${s.anchor} anchors · ${s.node} nodes · ${s.edge} edges`;
  } catch {
    $("stats").textContent = "";
  }
}

async function loadPredicateList(listId) {
  const predicates = await api("/api/predicates").catch(() => []);
  $(listId).innerHTML = predicates
    .map((p) => `<option value="${escapeHtml(p.label)}"></option>`)
    .join("");
}

function suggestEntities(input, listId) {
  input.addEventListener("input", async () => {
    const q = input.value.trim();
    if (!q) return;
    const nodes = await api(`/api/nodes?q=${encodeURIComponent(q)}`).catch(() => []);
    $(listId).innerHTML = nodes
      .map((n) => `<option value="${escapeHtml(n.label)}"></option>`)
      .join("");
  });
}

// Assert or update an edge; on an unknown predicate, offer the deliberate
// extra step (create it) and retry once.
async function submitEdge(path, body, method) {
  try {
    return await postJson(path, body, method);
  } catch (err) {
    if (!err.message.startsWith("unknown predicate")) throw err;
    const label = normalizePredicate(body.edge_type);
    if (!window.confirm(`Predicate ${label} does not exist yet. Create it?`)) throw err;
    return postJson(path, { ...body, create_predicate: true }, method);
  }
}
