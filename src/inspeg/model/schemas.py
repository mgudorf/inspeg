"""Selector types (after the W3C Web Annotation Data Model) and API payloads."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

PROVENANCE_TIERS = ("exact", "sourced", "attributed", "orphan")


class TextPositionSelector(BaseModel):
    """Character offsets into the decoded text of one artifact (oa:TextPositionSelector)."""

    type: Literal["text_position"] = "text_position"
    start: int = Field(ge=0)
    end: int = Field(ge=0)


class AssertEdgeRequest(BaseModel):
    src_label: str = Field(min_length=1)
    edge_type: str = Field(min_length=1)
    dst_label: str = Field(min_length=1)
    anchor_id: str | None = None  # None = manual, unevidenced assertion
    note: str | None = None
    create_predicate: bool = False  # the deliberate extra step (ADR 0003)


class UpdateEdgeRequest(BaseModel):
    src_label: str = Field(min_length=1)
    edge_type: str = Field(min_length=1)
    dst_label: str = Field(min_length=1)
    note: str | None = None
    create_predicate: bool = False


class PredicateCreate(BaseModel):
    label: str = Field(min_length=1)


class NodeOut(BaseModel):
    id: str
    label: str


class EdgeOut(BaseModel):
    id: str
    src: NodeOut
    type: NodeOut
    dst: NodeOut
    anchor_id: str | None = None
    note: str | None = None


class EdgeRow(BaseModel):
    """One row of the graph table: labels denormalized, evidence counted."""

    id: str
    src: NodeOut
    type: str
    dst: NodeOut
    note: str | None = None
    evidence: int
    anchor_id: str | None = None


class CaptureOut(BaseModel):
    anchor_id: str
    artifact_id: str
    sibling_artifact_ids: list[str] = Field(default_factory=list)
    provenance: str
    source_url: str | None = None
    source_app: str | None = None
    captured_at: str
    excerpt: str
