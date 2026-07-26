"use strict";

const $ = (id) => document.getElementById(id);

let anchorId = new URLSearchParams(location.search).get("anchor");

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

function escapeHtml(s) {
  return s.replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`);
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

async function loadAnchor() {
  let detail;
  try {
    detail = await api(anchorId ? `/api/anchors/${anchorId}` : "/api/anchors/latest");
  } catch {
    $("empty").classList.remove("hidden");
    return;
  }
  anchorId = detail.anchor.id;

  const art = detail.artifact;
  const tier = $("tier");
  tier.textContent = TIER_LABELS[art.provenance] || art.provenance;
  tier.dataset.tier = art.provenance;

  if (art.source_uri) {
    const link = $("source");
    link.href = art.source_uri;
    link.title = art.source_uri;
    try {
      link.textContent = new URL(art.source_uri).hostname;
    } catch {
      link.textContent = art.source_uri;
    }
    link.classList.remove("hidden");
  }
  if (art.source_app) $("app").textContent = art.source_app;
  $("when").textContent = new Date(art.captured_at).toLocaleString();
  $("excerpt").textContent = detail.excerpt || "(no text)";

  $("capture").classList.remove("hidden");
  $("assert").classList.remove("hidden");
  $("src").focus();
}

function suggest(input, listId, kind) {
  input.addEventListener("input", async () => {
    const q = input.value.trim();
    if (!q) return;
    const kindParam = kind ? `&kind=${kind}` : "";
    const nodes = await api(`/api/nodes?q=${encodeURIComponent(q)}${kindParam}`).catch(() => []);
    $(listId).innerHTML = nodes
      .map((n) => `<option value="${escapeHtml(n.label)}"></option>`)
      .join("");
  });
}

function flash(message, ok) {
  const el = $("flash");
  el.textContent = message;
  el.className = `flash ${ok ? "ok" : "err"}`;
  if (ok) setTimeout(() => (el.textContent = ""), 2500);
}

function addToSession(edge) {
  const li = document.createElement("li");
  li.innerHTML =
    `${escapeHtml(edge.src.label)} —` +
    `<span class="t">${escapeHtml(edge.type.label)}</span>→ ` +
    `${escapeHtml(edge.dst.label)}`;
  $("session").prepend(li);
}

$("assert").addEventListener("submit", async (event) => {
  event.preventDefault();
  const body = {
    anchor_id: anchorId,
    src_label: $("src").value,
    edge_type: $("type").value,
    dst_label: $("dst").value,
    note: $("note").value || null,
  };
  try {
    const edge = await api("/api/edges", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    flash("asserted ✓", true);
    addToSession(edge);
    $("src").value = "";
    $("dst").value = "";
    $("note").value = "";
    $("src").focus(); // predicate is kept — it usually repeats
    loadStats();
  } catch (err) {
    flash(err.message, false);
  }
});

suggest($("src"), "entity-list", null);
suggest($("dst"), "entity-list", null);
suggest($("type"), "type-list", "edge_type");

loadStats();
loadAnchor();
