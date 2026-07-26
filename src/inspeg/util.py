"""Small shared helpers."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def utcnow_iso() -> str:
    """Current UTC time as ISO 8601 with millisecond precision."""
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


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
