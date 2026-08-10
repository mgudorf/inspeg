---
name: surface-smoke
description: End-to-end smoke of every capture surface against a live daemon, plus the manual checklist CI cannot run (real Chrome menu, VS Code menus, HUD). Run after changing any surface and after Chrome/VS Code major updates — this is the surface-rot detector.
---

# Surface smoke test

CI proves the API; it cannot right-click. This skill does both halves.

## Automated half (run these)

1. Start a throwaway daemon:
   `python -m inspeg run --no-hotkey --no-hud --no-context-watch --data-dir <temp dir>`
   (use a scratch dir; never the real `~/.inspeg`).
2. `GET /api/health` → ok, correct `extension_origins`/`context_watch`.
3. POST one capture per surface and assert 200 + expected provenance:
   - `/api/captures/selection` with doc_text (browser HTML path → `exact`);
   - `/api/captures/selection` without doc_text (PDF path → `sourced`,
     response carries `document_artifact_id` starting `pt_`);
   - `/api/captures/pointer` kind=url (image/link) and kind=file;
   - `/api/captures/code` (→ `exact`).
   All need `X-Inspeg-Capture: 1`.
4. Read side: `/api/labels`, `/api/resolve?url=`, `/api/tree` (both
   group_bys), `/api/queue`, `/api/anchors/url-digests`, `/api/search?q=`
   (503 is correct if the daemon was started without FTS), `/hud/` serves
   with a CSP header.
5. `python -m inspeg reindex --data-dir <same dir>` after stopping the
   daemon → count > 0.

## Manual half (print this checklist for the user; you cannot do it)

- [ ] Chrome: select text on an article → right-click → Inspeg ▸ Capture as ▸
      shows recent labels; one click captures, toast appears, no window opens.
- [ ] Chrome: recapture on the same page after reload → highlight appears
      (re-anchoring works); second capture dedupes the Document artifact.
- [ ] Chrome PDF viewer (an arXiv PDF): selection capture works via the
      degraded path; label lands.
- [ ] Chrome: right-click an image and a link → pointer captures.
- [ ] VS Code: editor selection → both capture commands; file-tree →
      Capture file; reopening the file paints decorations.
- [ ] HUD: hotkey toggles; switching Chrome tab ↔ VS Code regroups the
      context band; label chip → items; open/reveal/vscode jumps land;
      redact removes the excerpt everywhere.
- [ ] After a Chrome major update: menus still render, capture still 200s
      (watch for Local Network Access changes; extension needs Chrome ≥ 144).
