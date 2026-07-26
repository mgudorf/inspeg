"use strict";

let edges = [];
let sortKey = null;
let sortDir = 1;
let editingId = null;

const KEY_FNS = {
  src: (e) => e.src.label.toLowerCase(),
  type: (e) => e.type.toLowerCase(),
  dst: (e) => e.dst.label.toLowerCase(),
};

async function loadEdges() {
  edges = await api("/api/edges").catch(() => []);
  render();
}

function render() {
  const rows = [...edges];
  if (sortKey) {
    const key = KEY_FNS[sortKey];
    rows.sort((a, b) => (key(a) < key(b) ? -sortDir : key(a) > key(b) ? sortDir : 0));
  }

  document.querySelectorAll("th.sortable").forEach((th) => {
    th.dataset.dir = th.dataset.sort === sortKey ? (sortDir === 1 ? "asc" : "desc") : "";
  });

  const tbody = document.querySelector("#edges tbody");
  tbody.innerHTML = rows
    .map(
      (e) => `
    <tr data-id="${escapeHtml(e.id)}">
      <td>${escapeHtml(e.src.label)}</td>
      <td class="predicate">${escapeHtml(e.type)}</td>
      <td>${escapeHtml(e.dst.label)}</td>
      <td class="dim">${e.note ? escapeHtml(e.note) : ""}</td>
      <td class="num">${
        e.evidence > 0 && e.anchor_id
          ? `<a href="/?anchor=${encodeURIComponent(e.anchor_id)}" title="open evidence">${e.evidence}</a>`
          : `<span class="none" title="no evidence anchor">0</span>`
      }</td>
      <td class="actions">
        <button type="button" class="ghost small" data-act="edit">edit</button>
        <button type="button" class="ghost small danger" data-act="del">delete</button>
      </td>
    </tr>`
    )
    .join("");
  $("none").classList.toggle("hidden", rows.length > 0);
}

document.querySelectorAll("th.sortable").forEach((th) => {
  th.addEventListener("click", () => {
    const key = th.dataset.sort;
    sortDir = sortKey === key ? -sortDir : 1;
    sortKey = key;
    render();
  });
});

document.querySelector("#edges tbody").addEventListener("click", async (event) => {
  const button = event.target.closest("button[data-act]");
  if (!button) return;
  const id = button.closest("tr").dataset.id;
  const edge = edges.find((e) => e.id === id);
  if (!edge) return;

  if (button.dataset.act === "del") {
    const label = `${edge.src.label} —${edge.type}→ ${edge.dst.label}`;
    if (!window.confirm(`Delete edge?\n\n${label}\n\n(The event log keeps the history.)`)) return;
    try {
      await api(`/api/edges/${encodeURIComponent(id)}`, { method: "DELETE" });
      await loadEdges();
      loadStats();
    } catch (err) {
      flash(err.message, false);
    }
  } else {
    startEdit(edge);
  }
});

function startEdit(edge) {
  editingId = edge.id;
  $("src").value = edge.src.label;
  $("type").value = edge.type;
  $("dst").value = edge.dst.label;
  $("note").value = edge.note || "";
  $("save").textContent = "Save edit";
  $("cancel").classList.remove("hidden");
  $("src").focus();
}

function resetForm() {
  editingId = null;
  ["src", "type", "dst", "note"].forEach((id) => ($(id).value = ""));
  $("save").textContent = "Add edge";
  $("cancel").classList.add("hidden");
}

$("cancel").addEventListener("click", resetForm);

$("edge-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const body = {
    src_label: $("src").value,
    edge_type: normalizePredicate($("type").value),
    dst_label: $("dst").value,
    note: $("note").value || null,
  };
  try {
    if (editingId) {
      await submitEdge(`/api/edges/${encodeURIComponent(editingId)}`, body, "PUT");
      flash("saved ✓", true);
    } else {
      await submitEdge("/api/edges", body);
      flash("added ✓", true);
    }
    resetForm();
    await loadEdges();
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
loadEdges();
