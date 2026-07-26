# Security

This is not boilerplate. inspeg reads the clipboard on demand and stores what
it captures unencrypted on disk. You should understand the model — and the
non-model — before using it.

[docs/security.md](docs/security.md) is the engineering log: every attack
vector that has been found and closed, the control that closed it, and the
test that fails if the control is removed. This file is the model itself.

## Threat model

**What inspeg does:**

- Reads the clipboard **only** when you press the capture hotkey or call the
  capture endpoint. There is no clipboard listener, no `WM_CLIPBOARDUPDATE`
  subscription, no polling. Nothing observes the clipboard unless invoked.
- Records the foreground application name and window title at capture time
  (for provenance tier 3).
- Binds to `127.0.0.1` only. A non-loopback `--host` is refused unless you
  pass `--allow-remote` and accept what that means. No network egress: it
  makes no outbound connections, phones nothing home, and has no telemetry.
- Stores everything locally under `~/.inspeg/` (or `--data-dir`).

**What defends the local API from the web.** A localhost port is not a
security boundary against a browser: any page you visit can send requests to
it. inspeg therefore rejects requests whose `Origin` does not match their own
`Host` (CSRF), rejects requests whose `Host` is not a loopback name (DNS
rebinding), and requires a custom `X-Inspeg-Capture` header on the capture
endpoint. Non-browser clients, which send no `Origin`, are unaffected. See
V1–V3 in [docs/security.md](docs/security.md).

**What inspeg does not do:**

- **No encryption at rest.** Blobs and the SQLite database are plain files.
  If you capture secrets, they sit on disk in the clear. Use full-disk
  encryption if that matters to you, and be deliberate about what you capture.
- **No authentication on the local API.** Any *process* on your machine that
  can reach `127.0.0.1:8137` can read your captures and write assertions.
  That is the standard posture of local daemons — such a process could read
  `~/.inspeg` directly anyway. Do not bind to other interfaces.
- **No sandboxing of captured content.** Captured HTML is stored verbatim.
  The UI renders extracted text (not HTML) and only ever links `http(s)`
  URLs, but exports you build later may carry whatever you captured.

**You are responsible for what you capture.** The tool is manual by
construction precisely so that nothing enters the store without you pressing
something.

## If you capture something you should not have

Redaction destroys an artifact's content while keeping its provenance
skeleton — that something was captured, when, and from where — so assertions
that cite it do not silently lose their attribution:

```
curl -X POST http://127.0.0.1:8137/api/artifacts/<sha256>/redact
```

The blob file is deleted and the artifact is flagged; `replay` will not
resurrect it. This is the only sanctioned exception to blob immutability
([ADR 0002](docs/adr/0002-redaction.md)). Note that redaction is per
*content*: identical bytes captured twice share one blob, so both captures
are redacted together.

Content that has already been exported, synced, or backed up is beyond the
tool's reach. Redact promptly.

## Reporting a vulnerability

Open a GitHub security advisory on this repository, or email
<matthew.gudorf@gmail.com>. Please do not open public issues for
security-sensitive reports.
