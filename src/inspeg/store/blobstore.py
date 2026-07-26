"""Content-addressed blob storage.

Blobs live on disk under ``<data_dir>/blobs/<first-two-hex>/<sha256>``, never in
the database. Deduplication is automatic: identical content maps to one file.
``artifact.path`` stores the data-dir-relative path.
"""

from __future__ import annotations

import hashlib
import os
import re
import uuid
from pathlib import Path

# Digests are validated everywhere they name a file: a digest is the only
# path component callers control, so this is the path-traversal boundary.
_DIGEST = re.compile(r"[0-9a-f]{64}")


class BlobStore:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = Path(data_dir)

    @staticmethod
    def relpath(digest: str) -> str:
        if not _DIGEST.fullmatch(digest):
            raise ValueError(f"not a sha256 hex digest: {digest!r}")
        return f"blobs/{digest[:2]}/{digest}"

    def put(self, data: bytes) -> tuple[str, str]:
        """Store bytes; return (sha256 hex digest, data-dir-relative path)."""
        digest = hashlib.sha256(data).hexdigest()
        rel = self.relpath(digest)
        path = self.data_dir / rel
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            # Unique per writer: concurrent puts of the same content must not
            # clobber each other's half-written temp file.
            tmp = path.parent / f"{digest}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
            tmp.write_bytes(data)
            os.replace(tmp, path)
        return digest, rel

    def get(self, digest: str) -> bytes:
        return (self.data_dir / self.relpath(digest)).read_bytes()

    def exists(self, digest: str) -> bool:
        return (self.data_dir / self.relpath(digest)).exists()

    def sweep(self, keep: set[str] | None = None) -> int:
        """Delete leftover ``*.tmp`` files and, when ``keep`` is given, any blob
        whose data-dir-relative path is not in it (rollback orphans, redacted
        content). Only call while holding the data-dir lock.
        """
        root = self.data_dir / "blobs"
        if not root.is_dir():
            return 0
        removed = 0
        for path in root.glob("*/*"):
            rel = f"blobs/{path.parent.name}/{path.name}"
            if path.name.endswith(".tmp") or (keep is not None and rel not in keep):
                path.unlink(missing_ok=True)
                removed += 1
        return removed
