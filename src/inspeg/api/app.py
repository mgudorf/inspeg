"""The single local endpoint: JSON API plus the quick-capture UI.

One process, one port, 127.0.0.1 only. Adapters are dumb clients of this API.

Browser-facing hardening (see docs/security.md for the attack analysis):

- **Host allowlist** (DNS rebinding): a malicious site can point its DNS at
  127.0.0.1 and read this API same-origin; requests whose Host header is not
  a loopback name are rejected with 400.
- **Same-origin enforcement** (CSRF): browsers attach an ``Origin`` header to
  every POST, including "simple" no-preflight ones. Any request whose Origin
  does not match its own Host is rejected with 403. Non-browser clients
  (curl, adapters) send no Origin and are unaffected.
- **Capture header** (CSRF, belt-and-braces): the clipboard-capture POST has
  no body, which makes it a CORS simple request — it additionally requires
  the custom ``X-Inspeg-Capture`` header, which cross-origin JavaScript
  cannot send without a preflight that will fail.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from inspeg import __version__, service
from inspeg.model.schemas import AssertEdgeRequest, CaptureOut, EdgeOut, NodeOut
from inspeg.store import Store
from inspeg.store import events as ev
from inspeg.util import resource_dir

# Starlette compares the Host header with the port stripped.
_LOOPBACK_HOSTS = ["127.0.0.1", "localhost"]


def create_app(
    store: Store,
    *,
    allow_remote: bool = False,
    hotkey_status: Callable[[], str] | None = None,
) -> FastAPI:
    app = FastAPI(
        title="inspeg",
        version=__version__,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
    )

    @app.middleware("http")
    async def reject_cross_origin(request: Request, call_next):
        origin = request.headers.get("origin")
        if origin is not None and origin != f"http://{request.headers.get('host', '')}":
            return JSONResponse({"detail": "cross-origin requests are rejected"}, status_code=403)
        return await call_next(request)

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
        }

    @app.get("/api/stats")
    def stats() -> dict:
        counts = {}
        for table in ("artifact", "anchor", "node", "edge", "event"):
            row = store.query_one(f"SELECT COUNT(*) AS c FROM {table}")
            counts[table] = row["c"] if row else 0
        return counts

    @app.post("/api/captures/clipboard", response_model=CaptureOut)
    def capture_clipboard(x_inspeg_capture: str | None = Header(None)) -> CaptureOut:
        if x_inspeg_capture is None:
            raise HTTPException(
                403, "missing X-Inspeg-Capture header (CSRF guard; send 'X-Inspeg-Capture: 1')"
            )
        if sys.platform != "win32":
            raise HTTPException(501, "clipboard capture is only available on Windows")
        from inspeg.adapters.clipboard import read_clipboard_snapshot

        try:
            capture = service.ingest_clipboard(store, read_clipboard_snapshot())
        except service.CaptureTooLargeError as exc:
            raise HTTPException(413, str(exc)) from exc
        except service.EmptyCaptureError as exc:
            raise HTTPException(400, str(exc)) from exc
        return CaptureOut(
            anchor_id=capture.anchor_id,
            artifact_id=capture.artifact_id,
            sibling_artifact_ids=list(capture.sibling_artifact_ids),
            provenance=capture.provenance,
            source_url=capture.source_url,
            source_app=capture.source_app,
            captured_at=capture.captured_at,
            excerpt=capture.excerpt,
        )

    def anchor_detail(anchor_id: str) -> dict:
        row = store.query_one(
            """SELECT a.id, a.artifact_id, a.selector_type, a.selector,
                      art.mimetype, art.provenance, art.source_uri, art.source_app,
                      art.captured_at, art.redacted
               FROM anchor a JOIN artifact art ON art.id = a.artifact_id
               WHERE a.id = ?""",
            (anchor_id,),
        )
        if row is None:
            raise HTTPException(404, f"unknown anchor: {anchor_id}")
        if row["redacted"]:
            excerpt = None
        else:
            try:
                text = store.blobs.get(row["artifact_id"]).decode("utf-8", "replace")
            except FileNotFoundError as exc:
                raise HTTPException(
                    410, f"blob for artifact {row['artifact_id']} is missing on disk"
                ) from exc
            selector = json.loads(row["selector"])
            piece = text[selector.get("start", 0) : selector.get("end", len(text))]
            if row["mimetype"] == "text/html":
                piece = service.html_to_text(piece[: service.EXCERPT_HTML_SLICE])
            excerpt = piece.strip()[: service.EXCERPT_LIMIT]
        return {
            "anchor": {
                "id": row["id"],
                "artifact_id": row["artifact_id"],
                "selector_type": row["selector_type"],
                "selector": json.loads(row["selector"]),
            },
            "artifact": {
                "id": row["artifact_id"],
                "mimetype": row["mimetype"],
                "provenance": row["provenance"],
                "source_uri": row["source_uri"],
                # Scheme-validated: the only value the UI may place in an href.
                "source_link": service.safe_url(row["source_uri"]),
                "source_app": row["source_app"],
                "captured_at": row["captured_at"],
                "redacted": bool(row["redacted"]),
            },
            "excerpt": excerpt,
        }

    @app.get("/api/anchors/latest")
    def latest_anchor() -> dict:
        row = store.query_one(
            "SELECT payload FROM event WHERE kind = ? ORDER BY seq DESC LIMIT 1",
            (ev.ANCHOR_ADDED,),
        )
        if row is None:
            raise HTTPException(404, "nothing captured yet")
        return anchor_detail(json.loads(row["payload"])["id"])

    @app.get("/api/anchors/{anchor_id}")
    def get_anchor(anchor_id: str) -> dict:
        return anchor_detail(anchor_id)

    @app.post("/api/artifacts/{artifact_id}/redact")
    def redact_artifact(artifact_id: str) -> dict:
        try:
            service.redact_artifact(store, artifact_id)
        except service.UnknownArtifactError as exc:
            raise HTTPException(404, f"unknown artifact: {artifact_id}") from exc
        return {"ok": True, "artifact_id": artifact_id}

    @app.get("/api/nodes", response_model=list[NodeOut])
    def search_nodes(q: str = "", kind: str | None = None, limit: int = 20) -> list[NodeOut]:
        limit = max(1, min(limit, 100))
        escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        rows = store.query(
            r"""SELECT id, label FROM node
                WHERE label LIKE ? ESCAPE '\'
                  AND json_extract(props, '$.kind') IS ?
                ORDER BY label LIMIT ?""",
            (f"{escaped}%", kind, limit),
        )
        return [NodeOut(id=row["id"], label=row["label"]) for row in rows]

    @app.post("/api/edges", response_model=EdgeOut)
    def post_edge(req: AssertEdgeRequest) -> EdgeOut:
        try:
            result = service.assert_edge(
                store,
                anchor_id=req.anchor_id,
                src_label=req.src_label,
                edge_type=req.edge_type,
                dst_label=req.dst_label,
                note=req.note,
            )
        except service.UnknownAnchorError as exc:
            raise HTTPException(404, f"unknown anchor: {req.anchor_id}") from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        return EdgeOut(**result)

    # Mounted last so /api/* routes win.
    app.mount("/", StaticFiles(directory=resource_dir("ui"), html=True), name="ui")
    return app
