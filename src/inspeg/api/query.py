"""Read routes: anchors, context resolve, tree, labels' items, similar items.

Everything here is served from the read-only snapshot connection (see
``queries``); redaction is honored and pointer artifacts are branched before
any blob access.
"""

from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException

from inspeg import queries, service
from inspeg.store import Store
from inspeg.store import events as ev


def query_router(store: Store) -> APIRouter:
    router = APIRouter()

    def anchor_detail(anchor_id: str) -> dict:
        row = store.read_query_one(
            """SELECT a.id, a.artifact_id, a.selector_type, a.selector,
                      art.kind, art.mimetype, art.provenance, art.source_uri,
                      art.source_title, art.source_app, art.locator,
                      art.captured_at, art.redacted
               FROM anchor a JOIN artifact art ON art.id = a.artifact_id
               WHERE a.id = ?""",
            (anchor_id,),
        )
        if row is None:
            raise HTTPException(404, f"unknown anchor: {anchor_id}")
        artifact = {
            "id": row["artifact_id"],
            "kind": row["kind"],
            "mimetype": row["mimetype"],
            "provenance": row["provenance"],
            "source_uri": row["source_uri"],
            # Scheme-validated: the only value the UI may place in an href.
            "source_link": service.safe_url(row["source_uri"]),
            "source_title": row["source_title"],
            "source_app": row["source_app"],
            "captured_at": row["captured_at"],
            "redacted": bool(row["redacted"]),
        }
        if row["kind"] == "pointer":
            # ADR 0005: pointers have no blob — branch BEFORE any blobs call
            # (a pt_ id through the digest check is a 500, not a 410).
            if row["locator"] and not row["redacted"]:
                artifact["locator"] = json.loads(row["locator"])
            excerpt = None
        elif row["redacted"]:
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
            "artifact": artifact,
            "excerpt": excerpt,
        }

    @router.get("/api/anchors/latest")
    def latest_anchor() -> dict:
        row = store.read_query_one(
            "SELECT payload FROM event WHERE kind = ? ORDER BY seq DESC LIMIT 1",
            (ev.ANCHOR_ADDED,),
        )
        if row is None:
            raise HTTPException(404, "nothing captured yet")
        return anchor_detail(json.loads(row["payload"])["id"])

    @router.get("/api/anchors/url-digests")
    def get_url_digests() -> dict:
        return {"digests": queries.url_digests(store)}

    @router.get("/api/anchors/{anchor_id}")
    def get_anchor(anchor_id: str) -> dict:
        return anchor_detail(anchor_id)

    @router.get("/api/resolve")
    def resolve(
        url: str | None = None,
        path: str | None = None,
        exe: str | None = None,
        key: str | None = None,
        limit: int = 50,
        cursor: int | None = None,
    ) -> dict:
        try:
            return queries.resolve_context(
                store, url=url, path=path, exe=exe, key=key, limit=limit, cursor=cursor
            )
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @router.get("/api/queue")
    def get_queue(limit: int = 20) -> dict:
        return {"items": queries.unannotated_queue(store, limit=limit)}

    @router.get("/api/tree")
    def get_tree(group_by: str = "context", limit: int = 50, offset: int = 0) -> dict:
        try:
            return queries.tree(store, group_by=group_by, limit=limit, offset=offset)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @router.get("/api/labels/{node_id}/items")
    def get_label_items(node_id: str, limit: int = 50, cursor: int | None = None) -> dict:
        return queries.label_items(store, node_id, limit=limit, cursor=cursor)

    @router.get("/api/items/{anchor_id}/similar")
    def get_similar(anchor_id: str, limit: int = 25) -> dict:
        if store.read_query_one("SELECT 1 FROM anchor WHERE id = ?", (anchor_id,)) is None:
            raise HTTPException(404, f"unknown anchor: {anchor_id}")
        return {"items": queries.similar_items(store, anchor_id, limit=limit)}

    return router
