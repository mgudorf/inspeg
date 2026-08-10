"""Deep-link dispatch (V17): the HUD's only way to launch external targets.

Two controls, both mutation-tested: a scheme allowlist (V6's lesson applied
to launching — never feed stored data to a shell), and same-origin-only
access (extension origins are excluded by the app middleware's path scoping
AND rechecked here; the ``X-Inspeg-Open`` header forces a failing preflight
for web pages, the V1 pattern).
"""

from __future__ import annotations

import os
from urllib.parse import urlsplit

from fastapi import APIRouter, Header, HTTPException, Request

from inspeg.model.schemas import OpenIn
from inspeg.util import canonical_file_path

OPEN_SCHEMES = frozenset({"http", "https", "vscode", "vscode-insiders"})


def dispatch_router(extension_origins: frozenset[str], opener=None) -> APIRouter:
    if opener is None:  # injectable for tests — nothing may actually launch there
        from inspeg.adapters import openurl as opener

    router = APIRouter()

    @router.post("/api/open")
    def open_target(
        req: OpenIn, request: Request, x_inspeg_open: str | None = Header(None)
    ) -> dict:
        if x_inspeg_open is None:
            raise HTTPException(
                403, "missing X-Inspeg-Open header (CSRF guard; send 'X-Inspeg-Open: 1')"
            )
        # Belt-and-braces: the middleware's path scoping already excludes
        # extension origins from this route.
        if request.headers.get("origin") in extension_origins:
            raise HTTPException(403, "extension origins may not dispatch deep links")
        if (req.url is None) == (req.reveal is None):
            raise HTTPException(422, "pass exactly one of url / reveal")
        if req.url is not None:
            try:
                scheme = urlsplit(req.url).scheme.lower()
            except ValueError:
                scheme = ""
            if scheme not in OPEN_SCHEMES:
                raise HTTPException(
                    422,
                    f"scheme {scheme or '?'!r} is not in the allowlist "
                    f"({', '.join(sorted(OPEN_SCHEMES))})",
                )
            opener.open_url(req.url)
            return {"ok": True, "opened": req.url}
        path = canonical_file_path(req.reveal or "")
        if not os.path.exists(path):
            raise HTTPException(404, f"no such path: {path}")
        try:
            opener.reveal_in_explorer(path)
        except NotImplementedError as exc:
            raise HTTPException(501, str(exc)) from exc
        return {"ok": True, "revealed": path}

    return router
