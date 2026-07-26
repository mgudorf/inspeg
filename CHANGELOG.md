# Changelog

All notable changes to this project will be documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] - 2026-07-26

### Added — M0 vertical slice

- Content-addressed blob store (`blobs/<aa>/<sha256>`) with automatic dedup.
- Append-only event log in SQLite; artifact/anchor/node/edge/support tables
  are a projection rebuilt by `replay`.
- CF_HTML clipboard parser: `SourceURL` detection for provenance tier 2,
  byte-to-char offset mapping for fragment anchors.
- Global capture hotkey (Ctrl+Alt+A, Windows) via `RegisterHotKey`; captures
  HTML and plain-text clipboard formats as sibling artifacts.
- Provenance tiers `sourced`/`attributed`/`orphan` assigned at capture time
  (tier 1 `exact` arrives with the browser extension in M1).
- FastAPI daemon on 127.0.0.1 serving the JSON API and the quick-capture UI.
- Quick-capture window: shows the captured span, its tier and source; asserts
  subject —predicate→ object triples with the anchor attached as evidence.
- Edge types are nodes (`props.kind = "edge_type"`), denormalized into
  `edge.type` for query speed.
- Schema migrations in `schema/`, applied by filename, tracked in
  `schema_migration`.
