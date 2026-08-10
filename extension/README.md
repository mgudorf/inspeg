# inspeg capture — Chrome extension

The in-browser capture surface (ADR 0007): right-click → **Inspeg ▸ Capture
as ▸ your label** on selections, images, links, and media; captured pages
re-highlight your quotes on revisit.

## Install (unpacked, once)

1. `chrome://extensions` → enable **Developer mode** → **Load unpacked** →
   pick this `extension/` directory.
2. The `key` in `manifest.json` pins the extension id to
   `hcoiigcfphgmjdpojodchgppadjkkkji`, so the id is stable across machines
   and reloads — that id is what the daemon allowlists.
3. Start the daemon with the origin allowlisted:

   ```
   python -m inspeg --extension-origin chrome-extension://hcoiigcfphgmjdpojodchgppadjkkkji
   ```

   The extension's options page shows connection + allowlist status.

## What captures what

| Gesture | Stored |
|---|---|
| Select text → Capture as ▸ label | Document artifact (full page text, deduped by hash) + `text_quote` **and** `text_position` selectors + selection HTML — provenance `exact` |
| Right-click image / link / video / audio | Pointer artifact (`kind:url`) + page provenance — no bytes copied |
| Select text in Chrome's PDF viewer | Selection text + a pointer to the PDF — provenance `sourced` (the viewer runs no content scripts; see the plan's PDF path) |

Local PDFs need "Allow access to file URLs" on `chrome://extensions` if you
want the PDF path to work on `file://` pages.

## Privacy properties

- No `tabs` permission: capture provenance comes from `OnClickData.pageUrl`,
  which costs nothing.
- Highlight-on-revisit never sends your browsing history to the daemon: the
  daemon publishes SHA-256 digests of *captured* URLs; pages are checked
  locally and only a hit triggers a resolve call.
- The active-tab context report (options page) is opt-in, ephemeral on the
  daemon side (ADR 0004), and off by default.

## Vendored anchoring (never hand-written)

`anchoring/approx-string-match.js` — approx-string-match 2.0.0, MIT,
verbatim except the ESM→global wrapper. `anchoring/match-quote.js` — ported
from Hypothesis client (BSD-2-Clause), scoring model verbatim.
`anchoring/text-range.js` — mechanical offset↔Range bookkeeping only.
Update by re-vendoring; verify licenses at pin time.
