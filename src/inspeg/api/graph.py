"""Graph routes: nodes, predicates, edges, and the label primitive."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response

from inspeg import queries, service
from inspeg.api.deps import require_capture_header
from inspeg.model.schemas import (
    AssertEdgeRequest,
    EdgeOut,
    EdgeRow,
    LabelIn,
    NodeOut,
    PredicateCreate,
    UpdateEdgeRequest,
)
from inspeg.store import Store


def _unknown_predicate_detail(exc: Exception) -> str:
    # The "unknown predicate" prefix is a contract with the UI, which offers
    # the create-and-retry step when it sees it.
    label = exc.args[0] if exc.args else "?"
    return (
        f"unknown predicate {label!r}: predicates are a controlled vocabulary — "
        "create it first (POST /api/predicates) or pass create_predicate=true"
    )


def graph_router(store: Store) -> APIRouter:
    router = APIRouter()

    @router.get("/api/nodes", response_model=list[NodeOut])
    def search_nodes(q: str = "", kind: str | None = None, limit: int = 20) -> list[NodeOut]:
        limit = max(1, min(limit, 100))
        escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        rows = store.read_query(
            r"""SELECT id, label FROM node
                WHERE label LIKE ? ESCAPE '\'
                  AND json_extract(props, '$.kind') IS ?
                ORDER BY label LIMIT ?""",
            (f"{escaped}%", kind, limit),
        )
        return [NodeOut(id=row["id"], label=row["label"]) for row in rows]

    @router.get("/api/graph/search")
    def graph_search(q: str = "", limit: int = 25) -> dict:
        """Search every node kind for the graph viewer (same-origin only)."""
        return {"nodes": queries.search_graph_nodes(store, q, limit=limit)}

    @router.get("/api/graph/nodes/{node_id}")
    def graph_node(node_id: str) -> dict:
        """One node's hyperlinked neighborhood: edges both ways + co-labels."""
        detail = queries.node_detail(store, node_id)
        if detail is None:
            raise HTTPException(404, f"unknown node: {node_id}")
        return detail

    @router.get("/api/predicates", response_model=list[NodeOut])
    def get_predicates() -> list[NodeOut]:
        return [NodeOut(**row) for row in service.list_predicates(store)]

    @router.post("/api/predicates", response_model=NodeOut)
    def post_predicate(req: PredicateCreate) -> NodeOut:
        try:
            return NodeOut(**service.create_predicate(store, req.label))
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @router.get("/api/edges", response_model=list[EdgeRow])
    def get_edges(response: Response, limit: int = 100, cursor: int | None = None) -> list[EdgeRow]:
        rows, next_cursor = service.list_edges(store, limit=limit, cursor=cursor)
        if next_cursor is not None:
            response.headers["X-Next-Cursor"] = str(next_cursor)
        return [EdgeRow(**row) for row in rows]

    @router.post("/api/edges", response_model=EdgeOut)
    def post_edge(req: AssertEdgeRequest) -> EdgeOut:
        try:
            result = service.assert_edge(
                store,
                anchor_id=req.anchor_id,
                src_label=req.src_label,
                edge_type=req.edge_type,
                dst_label=req.dst_label,
                note=req.note,
                create_predicate=req.create_predicate,
            )
        except service.UnknownAnchorError as exc:
            raise HTTPException(404, f"unknown anchor: {req.anchor_id}") from exc
        except service.UnknownPredicateError as exc:
            raise HTTPException(422, _unknown_predicate_detail(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        return EdgeOut(**result)

    @router.put("/api/edges/{edge_id}", response_model=EdgeOut)
    def put_edge(edge_id: str, req: UpdateEdgeRequest) -> EdgeOut:
        try:
            result = service.update_edge(
                store,
                edge_id,
                src_label=req.src_label,
                edge_type=req.edge_type,
                dst_label=req.dst_label,
                note=req.note,
                create_predicate=req.create_predicate,
            )
        except service.UnknownEdgeError as exc:
            raise HTTPException(404, f"unknown edge: {edge_id}") from exc
        except service.UnknownPredicateError as exc:
            raise HTTPException(422, _unknown_predicate_detail(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        return EdgeOut(**result)

    @router.delete("/api/edges/{edge_id}")
    def delete_edge(edge_id: str) -> dict:
        try:
            service.retract_edge(store, edge_id)
        except service.UnknownEdgeError as exc:
            raise HTTPException(404, f"unknown edge: {edge_id}") from exc
        return {"ok": True, "edge_id": edge_id}

    @router.get("/api/labels")
    def get_labels(sort: str = "recent", limit: int = 10) -> list[dict]:
        if sort not in ("recent", "frequent"):
            raise HTTPException(422, "sort must be 'recent' or 'frequent'")
        return service.list_labels(store, sort=sort, limit=limit)

    @router.post("/api/anchors/{anchor_id}/labels", dependencies=[Depends(require_capture_header)])
    def post_label(anchor_id: str, req: LabelIn) -> dict:
        try:
            return service.apply_label(store, anchor_id, req.label, surface=req.surface)
        except service.UnknownAnchorError as exc:
            raise HTTPException(404, f"unknown anchor: {anchor_id}") from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @router.delete(
        "/api/anchors/{anchor_id}/labels", dependencies=[Depends(require_capture_header)]
    )
    def delete_label(anchor_id: str, label: str) -> dict:
        try:
            service.remove_label(store, anchor_id, label)
        except service.UnknownLabelError as exc:
            raise HTTPException(404, f"label not on this anchor: {label}") from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        return {"ok": True}

    return router
