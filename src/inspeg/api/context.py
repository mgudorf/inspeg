"""Ephemeral context endpoints (ADR 0004 / V16).

When the watcher is disabled these routes REFUSE (403) rather than return
empty — "off" must be verifiable from the outside, not indistinguishable
from "nothing happening". Nothing here can reach the Store: the router
receives only the ContextHub.
"""

from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException

from inspeg.context import ContextHub
from inspeg.model.schemas import TabContextIn, WorkspaceContextIn


def context_router(hub: ContextHub | None) -> APIRouter:
    router = APIRouter()

    def require_hub() -> ContextHub:
        if hub is None:
            raise HTTPException(
                403, "context watch is disabled (--no-context-watch); endpoints refuse"
            )
        return hub

    def require_context_header(x_inspeg_context: str | None) -> None:
        if x_inspeg_context is None:
            raise HTTPException(
                403, "missing X-Inspeg-Context header (CSRF guard; send 'X-Inspeg-Context: 1')"
            )

    @router.get("/api/context")
    def get_context() -> dict:
        return require_hub().snapshot()

    @router.post("/api/context/tab")
    def post_tab(req: TabContextIn, x_inspeg_context: str | None = Header(None)) -> dict:
        require_context_header(x_inspeg_context)
        require_hub().set_tab(req.url, req.title)
        return {"ok": True}

    @router.post("/api/context/workspace")
    def post_workspace(
        req: WorkspaceContextIn, x_inspeg_context: str | None = Header(None)
    ) -> dict:
        require_context_header(x_inspeg_context)
        require_hub().set_workspace(req.root, req.file)
        return {"ok": True}

    return router
