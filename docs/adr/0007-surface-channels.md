# ADR 0007 — Surface channels and identity: how capture surfaces reach the daemon

- Status: accepted
- Date: 2026-08-08

## Context

The plan adds two capture surfaces (Chrome extension, VS Code extension) and
two local clients (HUD, existing hotkey). The daemon's browser hardening
(V1 same-origin rejection, V2 Host allowlist, V1 capture header — see
`docs/security.md`) currently 403s *every* browser-extension request, because
Chrome MV3 service workers send `Origin: chrome-extension://<id>`. Something
must change, and the change must not reopen V1/V2 for hostile web pages.

## Decision

One writer (the daemon), one transport (HTTP loopback), three channel
classes:

1. **Chrome extension** — requests carry `Origin: chrome-extension://<pinned
   id>`. The daemon accepts an Origin **exactly matching** a configured
   allowlist entry (`--extension-origin`, repeatable, persisted in config),
   in addition to the existing same-origin rule. Constraints:
   - never a wildcard; `chrome-extension://*` is refused at config-parse time;
   - **no API may ever mutate the allowlist** — it is config-file/flag only
     (a network-writable allowlist would be a self-modifying security policy);
   - the `X-Inspeg-Capture` header stays mandatory on every write route;
   - a narrowly scoped OPTIONS preflight responder echoes the one matched
     allowlisted origin — never `*` — and only the headers the extension
     needs;
   - the extension pins `"key"` in its manifest so its ID (and thus origin)
     is stable; that ID is what the user allowlists.
   A hostile web page gains nothing: pages cannot forge `chrome-extension://`
   origins, non-allowlisted extensions get the same 403, and the preflight
   still fails for web origins. V1/V2 tests stay green, and every new branch
   gets its own mutation-verified test (V15).
2. **VS Code extension** — Node `fetch` to loopback with the capture header
   and **no `Origin`**, which passes `reject_cross_origin` unchanged. This is
   indistinguishable from `curl` — the already-accepted local-process risk —
   so no control is weakened and no new one is needed.
3. **HUD** — same-origin (it renders pages served from
   `http://127.0.0.1:8137`), exactly like today's UI.

Every human-initiated write keeps `actor="human"` — invariant #3 separates
human from proposer, not surface from surface. The **surface**
(`browser | vscode | hud | hotkey`) is recorded in event payloads for
provenance and analytics.

**Native messaging** (a stdio host process, the KeePassXC-proxy pattern) is
recorded as an optional hardening step: it would retire V1/V2 entirely for
the browser channel (no listening-port exposure, OS-attested caller
identity). It is not required for correctness and is deferred to the
hardening phase.

## Alternatives rejected

- **Native messaging as the primary transport.** A registry-registered host
  manifest plus one spawned process per connection, for a channel the
  loopback API must expose anyway (HUD, VS Code, curl). Complexity without
  removing the port.
- **A pairing endpooint that appends observed origins to the allowlist.** A
  self-modifying security policy driven by a network request; brute-forceable.
  Killed on review.
- **A shared secret/token for the extension.** Extension storage is readable
  by any same-user process, so the token adds no attacker the origin check
  does not already stop, and it complicates rotation.

## Consequences

- `reject_cross_origin` in `api/app.py` is restructured, not relaxed:
  middleware order (TrustedHost outermost) is preserved and the new branch is
  additive.
- `docs/security.md` V15 documents the allowlist with one test per behavior;
  the extension joins the trusted computing base and that residual risk is
  documented (an unpacked extension directory is same-user-writable).
- Event payloads gain a `surface` field going forward; replay tolerates its
  absence in old events.
