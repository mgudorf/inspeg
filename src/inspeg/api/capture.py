"""Write routes: captures, labels-on-capture, redaction, source upgrades.

Every route here funnels into ``service`` (the only writers) and requires the
``X-Inspeg-Capture`` header (V1/V15).
"""

from __future__ import annotations

import sys

from fastapi import APIRouter, Depends, HTTPException

from inspeg import service
from inspeg.api.deps import require_capture_header
from inspeg.model.schemas import (
    CaptureOut,
    CodeCaptureIn,
    PointerCaptureIn,
    SelectionCaptureIn,
    SourceUpgradeIn,
)
from inspeg.store import Store


def capture_router(store: Store) -> APIRouter:
    router = APIRouter(dependencies=[Depends(require_capture_header)])

    @router.post("/api/captures/clipboard", response_model=CaptureOut)
    def capture_clipboard() -> CaptureOut:
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

    @router.post("/api/captures/selection")
    def capture_selection(req: SelectionCaptureIn) -> dict:
        try:
            return service.ingest_web_capture(
                store,
                url=req.url,
                title=req.title,
                doc_text=req.doc_text,
                selection_exact=req.selection_exact,
                selection_prefix=req.selection_prefix,
                selection_suffix=req.selection_suffix,
                selection_start=req.selection_start,
                selection_end=req.selection_end,
                selection_html=req.selection_html,
                labels=req.labels,
            )
        except service.CaptureTooLargeError as exc:
            raise HTTPException(413, str(exc)) from exc
        except service.EmptyCaptureError as exc:
            raise HTTPException(400, str(exc)) from exc
        except (service.InvalidPointerError, ValueError) as exc:
            raise HTTPException(422, str(exc)) from exc

    @router.post("/api/captures/pointer")
    def capture_pointer(req: PointerCaptureIn) -> dict:
        try:
            return service.capture_pointer(
                store,
                kind=req.kind,
                target=req.target,
                mimetype=req.mimetype,
                page_uri=req.page_uri,
                source_title=req.source_title,
                labels=req.labels,
                surface=req.surface,
            )
        except (service.InvalidPointerError, ValueError) as exc:
            raise HTTPException(422, str(exc)) from exc

    @router.post("/api/captures/code")
    def capture_code(req: CodeCaptureIn) -> dict:
        try:
            return service.ingest_code_capture(
                store,
                text=req.text,
                path=req.path,
                start_line=req.start_line,
                start_col=req.start_col,
                end_line=req.end_line,
                end_col=req.end_col,
                workspace=req.workspace,
                git_remote=req.git_remote,
                git_commit=req.git_commit,
                labels=req.labels,
            )
        except service.CaptureTooLargeError as exc:
            raise HTTPException(413, str(exc)) from exc
        except service.EmptyCaptureError as exc:
            raise HTTPException(400, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @router.post("/api/artifacts/{artifact_id}/redact")
    def redact_artifact(artifact_id: str) -> dict:
        try:
            service.redact_artifact(store, artifact_id)
        except service.UnknownArtifactError as exc:
            raise HTTPException(404, f"unknown artifact: {artifact_id}") from exc
        return {"ok": True, "artifact_id": artifact_id}

    @router.delete("/api/artifacts/{artifact_id}")
    def delete_artifact(artifact_id: str) -> dict:
        """Hard delete (ADR 0010): content, anchors, and labels all go; the
        log keeps only an id tombstone. Same-origin + capture header, and
        deliberately NOT on the extension route allowlist."""
        try:
            service.delete_artifact(store, artifact_id)
        except service.UnknownArtifactError as exc:
            raise HTTPException(404, f"unknown artifact: {artifact_id}") from exc
        return {"ok": True, "artifact_id": artifact_id, "deleted": True}

    @router.post("/api/artifacts/{artifact_id}/source")
    def upgrade_source(artifact_id: str, req: SourceUpgradeIn) -> dict:
        """Attach a source to a sourceless capture. Deliberate user action —
        automated late upgrades belong in the proposal flow (invariant #3)."""
        try:
            upgraded = service.upgrade_artifact_source(
                store,
                artifact_id,
                source_uri=req.source_uri,
                source_title=req.source_title,
            )
        except service.UnknownArtifactError as exc:
            raise HTTPException(404, f"unknown artifact: {artifact_id}") from exc
        except service.InvalidPointerError as exc:
            raise HTTPException(422, str(exc)) from exc
        return {"ok": True, "artifact_id": artifact_id, "upgraded": upgraded}

    return router
