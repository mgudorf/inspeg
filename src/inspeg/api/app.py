"""The single local endpoint: JSON API plus the quick-capture UI.

One process, one port, 127.0.0.1 only. Adapters are dumb clients of this API.
"""

from __future__ import annotations

import json
import sys

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles

from inspeg import __version__, service
from inspeg.model.schemas import AssertEdgeRequest, CaptureOut, EdgeOut, NodeOut
from inspeg.store import Store
from inspeg.store import events as ev
from inspeg.util import resource_dir


def create_app(store: Store) -> FastAPI:
    app = FastAPI(
        title="inspeg",
        version=__version__,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
    )

    @app.get("/api/health")
    def health() -> dict:
        return {"ok": True, "version": __version__}

    @app.get("/api/stats")
    def stats() -> dict:
        counts = {}
        for table in ("artifact", "anchor", "node", "edge", "event"):
            row = store.query_one(f"SELECT COUNT(*) AS c FROM {table}")
            counts[table] = row["c"] if row else 0
        return counts

    @app.post("/api/captures/clipboard", response_model=CaptureOut)
    def capture_clipboard() -> CaptureOut:
        if sys.platform != "win32":
            raise HTTPException(501, "clipboard capture is only available on Windows")
        from inspeg.adapters.clipboard import read_clipboard_snapshot

        try:
            capture = service.ingest_clipboard(store, read_clipboard_snapshot())
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
                      art.captured_at
               FROM anchor a JOIN artifact art ON art.id = a.artifact_id
               WHERE a.id = ?""",
            (anchor_id,),
        )
        if row is None:
            raise HTTPException(404, f"unknown anchor: {anchor_id}")
        selector = json.loads(row["selector"])
        text = store.blobs.get(row["artifact_id"]).decode("utf-8", "replace")
        piece = text[selector.get("start", 0) : selector.get("end", len(text))]
        excerpt = service.html_to_text(piece) if row["mimetype"] == "text/html" else piece
        return {
            "anchor": {
                "id": row["id"],
                "artifact_id": row["artifact_id"],
                "selector_type": row["selector_type"],
                "selector": selector,
            },
            "artifact": {
                "id": row["artifact_id"],
                "mimetype": row["mimetype"],
                "provenance": row["provenance"],
                "source_uri": row["source_uri"],
                "source_app": row["source_app"],
                "captured_at": row["captured_at"],
            },
            "excerpt": excerpt.strip()[: service.EXCERPT_LIMIT],
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
