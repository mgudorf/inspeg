# ADR 0005 — Pointer artifacts: referencing content without copying it

- Status: accepted
- Date: 2026-08-08

## Context

The plan adds captures whose subject is not a byte payload on the clipboard
but a *thing elsewhere*: an image or link on a web page, a file in a VS Code
workspace, an audio/video file that must never be copied into the blob store
(user requirement: metadata pointers only for A/V). Content-addressed ids
(`sha256` of the bytes, `blobstore.put`) require bytes; pointers have none.

## Decision

An artifact may reference external content instead of holding a blob.

- **`artifact.kind`** distinguishes `'blob'` (existing) from `'pointer'`.
  `artifact.path` becomes nullable with `CHECK ((kind='blob') = (path IS NOT
  NULL))`.
- **Pointer id** = `pt_` + sha256 hex of `canonical_json({kind, target})`
  where `target` is the *stable identity only*: the normalized URL
  (`util.normalize_source_uri`) for `kind:'url'`, or the canonicalized
  absolute path for `kind:'file'`. Volatile facts — `byte_len`, `mtime`,
  `content_sha256` — ride the event payload and the `artifact.locator` JSON
  column, **never** the id, so re-capturing the same file after a metadata
  change dedupes to the same artifact instead of minting a new one.
- The `pt_` prefix cannot match `blobstore._DIGEST` (`[0-9a-f]{64}`) or
  `service._BLOB_RELPATH`, so the startup sweep and the redaction unlink
  guard are inherently safe: redacting a pointer flags the row and deletes
  nothing.
- Files smaller than 256 MiB get a streamed `content_sha256` in `locator`
  (identity/rot detection). Audio/video mimetypes are always pointer-only and
  never hashed or copied.
- **Every read path must branch on `artifact.kind` before touching the blob
  store.** Passing a `pt_` id to `store.blobs.get` fails the digest
  validation with `ValueError` — i.e. an unhandled 500, not the 410 a missing
  blob produces. A regression test pins the branch.

## Alternatives rejected

- **Hash the full descriptor (target + size + mtime) into the id.** Touching
  a file would mint a new artifact and silently break dedupe; the log would
  fill with near-duplicates of the same referent.
- **Download/copy the target and content-address it.** Violates the explicit
  A/V pointer-only requirement, races the network/filesystem at capture time,
  and turns a metadata gesture into a bulk copy.
- **A separate `pointer` table.** Anchors, support rows, provenance tiers,
  and redaction all already hang off `artifact`; a parallel table would
  duplicate every one of those relationships.

## Consequences

- `schema/0004_artifact_v2.sql` rebuilds `artifact` (replay repopulates it —
  ADR 0001's "replay, don't migrate").
- Pointer captures ride the existing `artifact_added` event kind with
  extended payload; old binaries replaying a new log tolerate the extra keys.
- Provenance tiers apply unchanged: a URL pointer with a known page is
  `sourced`; a workspace file pointer is `attributed`; the ladder is decided
  by the capturing service function, not by the pointer mechanism.
- The blob-store keep-set used by the startup sweep filters `path IS NOT
  NULL`.
