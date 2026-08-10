#!/usr/bin/env python
"""Pre-commit guard for the two mechanically-checkable hard invariants.

1. Applied migrations are never edited (invariant #6): any staged
   modification/deletion/rename of an existing ``schema/*.sql`` fails.
   New migration files pass.
2. Projection tables are written only inside ``src/inspeg/store/``
   (invariant #1): INSERT/UPDATE/DELETE against a projection table anywhere
   else fails — captures go through service.py -> Store.record.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

PROJECTION_TABLES = "artifact|anchor|node|node_alias|edge|support|capture_member"
WRITE_PATTERN = re.compile(
    rf"\b(INSERT\s+(?:OR\s+\w+\s+)?INTO|UPDATE|DELETE\s+FROM)\s+({PROJECTION_TABLES})\b",
    re.IGNORECASE,
)
ALLOWED_WRITER_PREFIX = ("src/inspeg/store/", "scripts/")


def staged_modified() -> set[str]:
    output = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=MDR"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout
    return {line.strip().replace("\\", "/") for line in output.splitlines() if line.strip()}


def main(argv: list[str]) -> int:
    failures: list[str] = []
    modified = staged_modified()
    for name in argv:
        path = name.replace("\\", "/")
        if path.startswith("schema/") and path.endswith(".sql"):
            if path in modified:
                failures.append(
                    f"{path}: applied migrations are never edited (invariant #6) — "
                    "add a new numbered migration instead"
                )
            continue
        if path.endswith(".py") and not path.startswith(ALLOWED_WRITER_PREFIX):
            try:
                text = Path(name).read_text(encoding="utf-8")
            except OSError:
                continue
            for match in WRITE_PATTERN.finditer(text):
                line = text.count("\n", 0, match.start()) + 1
                failures.append(
                    f"{path}:{line}: direct {match.group(1).upper()} on projection table "
                    f"'{match.group(2)}' (invariant #1) — go through Store.record"
                )
    for failure in failures:
        print(failure, file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
