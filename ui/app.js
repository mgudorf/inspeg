"use strict";

let anchorId = new URLSearchParams(location.search).get("anchor");

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

  // Only source_link (scheme-validated server-side) may become an href;
  // source_uri is attacker-controllable clipboard data (javascript:, data:).
  if (art.source_link) {
    const link = $("source");
    link.href = art.source_link;
    link.title = art.source_link;
    try {
      link.textContent = new URL(art.source_link).hostname;
    } catch {
      link.textContent = art.source_link;
    }
    link.classList.remove("hidden");
  }
  if (art.source_app) $("app").textContent = art.source_app;
  $("when").textContent = new Date(art.captured_at).toLocaleString();
  $("excerpt").textContent = art.redacted ? "(redacted)" : detail.excerpt || "(no text)";

  $("capture").classList.remove("hidden");
  $("assert").classList.remove("hidden");
  $("src").focus();
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
    edge_type: normalizePredicate($("type").value),
    dst_label: $("dst").value,
    note: $("note").value || null,
  };
  try {
    const edge = await submitEdge("/api/edges", body);
    flash("asserted ✓", true);
    addToSession(edge);
    $("src").value = "";
    $("dst").value = "";
    $("note").value = "";
    $("src").focus(); // predicate is kept — it usually repeats
    loadStats();
    loadPredicateList("type-list");
  } catch (err) {
    flash(err.message, false);
  }
});

suggestEntities($("src"), "entity-list");
suggestEntities($("dst"), "entity-list");

loadStats();
loadPredicateList("type-list");
loadAnchor();
