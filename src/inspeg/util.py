"""Small shared helpers."""

from __future__ import annotations

import json
import ntpath
import os
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_PREDICATE_SEPARATORS = re.compile(r"[\s\-]+")

_DEFAULT_PORTS = {"http": 80, "https": 443}


def utcnow_iso() -> str:
    """Current UTC time as ISO 8601 with millisecond precision."""
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def normalize_predicate_label(label: str) -> str:
    """Mechanical normalization for predicate labels: trim, collapse runs of
    whitespace/hyphens into ``_``, uppercase. Charset *validation* lives in the
    service layer — this function must never raise, because the projection uses
    it to normalize historical events during replay.
    """
    return _PREDICATE_SEPARATORS.sub("_", label.strip()).upper()


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def normalize_source_uri(uri: str | None) -> str | None:
    """Canonical URL identity for "what do I have from here" (ADR 0008).

    Lowercase scheme and host, strip default ports, drop the fragment, sort
    query parameters, no trailing slash. Deliberately conservative: no host
    aliasing, no tracking-parameter stripping — anything smarter must stay a
    pure function or replay stops being deterministic. Returns ``None`` for
    anything unparseable; never raises (the projection calls this on
    historical events).
    """
    if not uri:
        return None
    try:
        parts = urlsplit(uri.strip())
    except ValueError:
        return None
    if not parts.scheme or not (parts.netloc or parts.path):
        return None
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    netloc = host
    if parts.port is not None and parts.port != _DEFAULT_PORTS.get(scheme):
        netloc = f"{host}:{parts.port}"
    path = parts.path.rstrip("/")
    query = urlencode(sorted(parse_qsl(parts.query, keep_blank_values=True)))
    return urlunsplit((scheme, netloc, path, query, ""))


def split_source_app(composite: str | None) -> tuple[str | None, str | None]:
    """Best-effort split of the legacy ``"exe | title"`` composite (ADR 0008).

    Either part may be missing. A lone part that looks like an executable name
    is treated as the exe, anything else as the title. Never raises — the
    projection uses this to derive structured columns from historical events.
    """
    if not composite or not composite.strip():
        return None, None
    exe, sep, title = composite.partition(" | ")
    if sep:
        return exe.strip() or None, title.strip() or None
    lone = composite.strip()
    if lone.lower().endswith(".exe"):
        return lone, None
    return None, lone


def canonical_file_path(path: str) -> str:
    """Stable identity for a file-pointer target (ADR 0005).

    Absolute, normalized, symlinks left alone (resolving them can change
    identity underneath the user). On Windows-style paths the drive letter is
    uppercased so ``c:`` and ``C:`` dedupe. Called at capture time only — the
    minted id rides the event, so replay never recomputes this.
    """
    absolute = os.path.normpath(os.path.abspath(os.path.expanduser(path)))
    drive, _ = ntpath.splitdrive(absolute)
    if drive and drive.endswith(":"):
        absolute = drive.upper() + absolute[len(drive) :]
    return absolute


def derive_context_key(uri_norm: str | None, exe: str | None, locator: dict | None) -> str | None:
    """Deterministic context bucket for an artifact (ADR 0008).

    Priority: url > file > app > nothing. Workspace keys are supplied
    explicitly by the code-capture path, never derived here. Pure and total —
    the projection calls this for events that predate context columns.
    """
    if uri_norm:
        return f"url:{uri_norm}"
    if locator and locator.get("kind") == "file" and locator.get("target"):
        return f"file:{locator['target']}"
    if exe:
        return f"app:{exe}"
    return None


def resource_dir(name: str) -> Path:
    """Locate a repo-level data directory ('schema', 'ui').

    Installed wheels carry these inside the package (hatch force-include);
    a source checkout keeps them at the repository root.
    """
    pkg = Path(__file__).resolve().parent
    bundled = pkg / name
    if bundled.is_dir():
        return bundled
    repo = pkg.parent.parent / name
    if repo.is_dir():
        return repo
    raise FileNotFoundError(f"resource directory not found: {name}")
