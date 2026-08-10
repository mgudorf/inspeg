"""The single local endpoint: JSON API plus the served UIs (quick capture, HUD).

One process, one port, 127.0.0.1 only. Adapters are dumb clients of this API.

Browser-facing hardening (see docs/security.md for the attack analysis):

- **Host allowlist** (V2, DNS rebinding): requests whose Host header is not a
  loopback name are rejected with 400. Outermost middleware, always.
- **Same-origin enforcement** (V1, CSRF): any request whose Origin does not
  match its own Host is rejected with 403 — with exactly one carve-out (V15):
  an Origin exactly matching a configured ``chrome-extension://<id>``
  allowlist entry, and only on the extension-permitted routes. Never a
  wildcard, and nothing at runtime can grow the allowlist (ADR 0007).
- **Capture header** (V1 belt-and-braces): write routes require the custom
  ``X-Inspeg-Capture`` header, which cross-origin JavaScript cannot send
  without a preflight that will fail.
- **CSP on served pages** (V18): daemon-served HTML runs only same-origin
  scripts — window titles and captured strings render as text, never markup.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from inspeg import __version__, queries
from inspeg.api.capture import capture_router
from inspeg.api.context import context_router
from inspeg.api.dispatch import dispatch_router
from inspeg.api.graph import graph_router
from inspeg.api.query import query_router
from inspeg.api.stream import EventBus, stream_router
from inspeg.context import ContextHub
from inspeg.store import Store
from inspeg.util import resource_dir

# Starlette compares the Host header with the port stripped.
_LOOPBACK_HOSTS = ["127.0.0.1", "localhost"]

# Chrome/Edge extension ids are 32 chars of a-p. Anything else — and any
# wildcard — is refused at startup, not at request time (ADR 0007).
_EXTENSION_ORIGIN = re.compile(r"^chrome-extension://[a-p]{32}$")

# The only routes an allowlisted extension origin may touch (least privilege;
# V15). Everything else treats an extension origin like any cross origin.
_EXTENSION_PATHS = re.compile(
    r"^/api/(captures/(selection|pointer)"
    r"|labels"
    r"|resolve"
    r"|anchors/url-digests"
    r"|anchors/[^/]+/labels"
    r"|artifacts/[^/]+/source"
    r"|context/tab)$"
)

_PREFLIGHT_HEADERS = "content-type, x-inspeg-capture, x-inspeg-context"

_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
)


def validate_extension_origins(origins: Sequence[str]) -> frozenset[str]:
    """Startup-time validation: exact ids only, wildcards refused loudly."""
    validated = set()
    for origin in origins:
        origin = origin.strip().rstrip("/")
        if not _EXTENSION_ORIGIN.fullmatch(origin):
            raise ValueError(
                f"invalid extension origin {origin!r}: must be chrome-extension://<32-char id> "
                "exactly — wildcards and patterns are refused (ADR 0007)"
            )
        validated.add(origin)
    return frozenset(validated)


def create_app(
    store: Store,
    *,
    allow_remote: bool = False,
    hotkey_status: Callable[[], str] | None = None,
    extension_origins: Sequence[str] = (),
    context_hub: ContextHub | None = None,
    fts=None,
    opener=None,
) -> FastAPI:
    ext_origins = validate_extension_origins(extension_origins)
    app = FastAPI(
        title="inspeg",
        version=__version__,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
    )
    bus = EventBus()
    app.state.event_bus = bus
    store.on_commit.append(bus.publish_store_events)
    if context_hub is not None:
        context_hub.on_change = lambda state: bus.publish_threadsafe(
            {"type": "context", "state": state}
        )

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response: Response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        if not request.url.path.startswith("/api/"):
            # Served pages only: the UIs must never execute captured or
            # context-derived strings (V18).
            response.headers["Content-Security-Policy"] = _CSP
        return response

    @app.middleware("http")
    async def reject_cross_origin(request: Request, call_next):
        origin = request.headers.get("origin")
        same_origin = origin is None or origin == f"http://{request.headers.get('host', '')}"
        from_extension = (
            origin in ext_origins and _EXTENSION_PATHS.match(request.url.path) is not None
        )
        if not same_origin and not from_extension:
            return JSONResponse({"detail": "cross-origin requests are rejected"}, status_code=403)
        if from_extension and request.method == "OPTIONS":
            # Scoped preflight: echo exactly the one matched origin — never *.
            return Response(
                status_code=204,
                headers={
                    "Access-Control-Allow-Origin": origin,
                    "Access-Control-Allow-Methods": "GET, POST, DELETE, OPTIONS",
                    "Access-Control-Allow-Headers": _PREFLIGHT_HEADERS,
                    "Access-Control-Max-Age": "600",
                    "Vary": "Origin",
                },
            )
        response = await call_next(request)
        if from_extension:
            response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Vary"] = "Origin"
        return response

    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=["*"] if allow_remote else _LOOPBACK_HOSTS,
    )

    @app.get("/api/health")
    def health() -> dict:
        return {
            "ok": True,
            "version": __version__,
            "hotkey": hotkey_status() if hotkey_status is not None else "disabled",
            "extension_origins": sorted(ext_origins),
            "context_watch": context_hub is not None,
            "ignored_exes": sorted(context_hub.ignored_exes) if context_hub is not None else [],
        }

    @app.get("/api/stats")
    def stats() -> dict:
        counts = {}
        for table in ("artifact", "anchor", "node", "edge", "event"):
            row = store.read_query_one(f"SELECT COUNT(*) AS c FROM {table}")
            counts[table] = row["c"] if row else 0
        return counts

    @app.get("/api/search")
    def search(q: str, limit: int = 25) -> dict:
        if fts is None:
            raise HTTPException(503, "search index is not enabled")
        try:
            hits = fts.search(q, limit=limit)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        items = []
        for hit in hits:
            row = store.read_query_one(
                "SELECT rowid, id, kind, mimetype, provenance, captured_at, source_uri,"
                " source_uri_norm, source_title, source_exe, context_key, locator, redacted"
                " FROM artifact WHERE id = ? AND redacted = 0",
                (hit["artifact_id"],),
            )
            if row is not None:
                item = queries._items_for_artifacts(store, [row])[0]
                items.append({"snippet": hit["snippet"], "item": item})
        return {"items": items}

    app.include_router(capture_router(store))
    app.include_router(graph_router(store))
    app.include_router(query_router(store))
    app.include_router(context_router(context_hub))
    app.include_router(dispatch_router(ext_origins, opener=opener))
    app.include_router(stream_router(bus))

    # Mounted last so /api/* routes win.
    app.mount("/", StaticFiles(directory=resource_dir("ui"), html=True), name="ui")
    return app
