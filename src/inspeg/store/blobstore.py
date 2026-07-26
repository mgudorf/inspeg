"""Content-addressed blob storage.

Blobs live on disk under ``<data_dir>/blobs/<first-two-hex>/<sha256>``, never in
the database. Deduplication is automatic: identical content maps to one file.
``artifact.path`` stores the data-dir-relative path.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path


class BlobStore:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = Path(data_dir)

    @staticmethod
    def relpath(digest: str) -> str:
        return f"blobs/{digest[:2]}/{digest}"

    def put(self, data: bytes) -> tuple[str, str]:
        """Store bytes; return (sha256 hex digest, data-dir-relative path)."""
        digest = hashlib.sha256(data).hexdigest()
        rel = self.relpath(digest)
        path = self.data_dir / rel
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_bytes(data)
            os.replace(tmp, path)
        return digest, rel

    def get(self, digest: str) -> bytes:
        return (self.data_dir / self.relpath(digest)).read_bytes()

    def exists(self, digest: str) -> bool:
        return (self.data_dir / self.relpath(digest)).exists()
