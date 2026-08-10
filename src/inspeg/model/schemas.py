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


class TextQuoteSelector(BaseModel):
    """Quote with disambiguating context (oa:TextQuoteSelector); survives page edits."""

    type: Literal["text_quote"] = "text_quote"
    exact: str = Field(min_length=1)
    prefix: str = ""
    suffix: str = ""


class CodeSpanSelector(BaseModel):
    """A line/column range in a source file, with optional git identity for rot detection."""

    type: Literal["code_span"] = "code_span"
    path: str = Field(min_length=1)
    start_line: int = Field(ge=1)
    start_col: int = Field(ge=0)
    end_line: int = Field(ge=1)
    end_col: int = Field(ge=0)
    git_remote: str | None = None
    git_commit: str | None = None


class MediaFragSelector(BaseModel):
    """A time range into audio/video (media fragments ``t=start,end``)."""

    type: Literal["media_frag"] = "media_frag"
    t_start: float | None = Field(default=None, ge=0)
    t_end: float | None = Field(default=None, ge=0)


class WholeItemSelector(BaseModel):
    """The entire artifact — pointer captures and whole-file references."""

    type: Literal["whole"] = "whole"


class BboxSelector(BaseModel):
    """A rectangular region of an image, in natural-pixel coordinates."""

    type: Literal["bbox"] = "bbox"
    x: float = Field(ge=0)
    y: float = Field(ge=0)
    w: float = Field(gt=0)
    h: float = Field(gt=0)


Selector = (
    TextPositionSelector
    | TextQuoteSelector
    | CodeSpanSelector
    | MediaFragSelector
    | WholeItemSelector
    | BboxSelector
)


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


LabelList = list[str]


class SelectionCaptureIn(BaseModel):
    """Browser selection capture (ADR 0007). ``doc_text`` present = HTML page
    (implicit Document artifact, tier exact); absent = the PDF-viewer path."""

    url: str = Field(min_length=1)
    title: str | None = None
    doc_text: str | None = None
    selection_exact: str = Field(min_length=1)
    selection_prefix: str = ""
    selection_suffix: str = ""
    selection_start: int | None = Field(default=None, ge=0)
    selection_end: int | None = Field(default=None, ge=0)
    selection_html: str | None = None
    labels: LabelList = Field(default_factory=list, max_length=20)


class PointerCaptureIn(BaseModel):
    kind: str = Field(pattern="^(url|file)$")
    target: str = Field(min_length=1)
    mimetype: str = "application/octet-stream"
    page_uri: str | None = None
    source_title: str | None = None
    labels: LabelList = Field(default_factory=list, max_length=20)
    surface: str = Field(default="browser", pattern="^(browser|vscode|hud)$")


class CodeCaptureIn(BaseModel):
    text: str = Field(min_length=1)
    path: str = Field(min_length=1)
    start_line: int = Field(ge=1)
    start_col: int = Field(ge=0)
    end_line: int = Field(ge=1)
    end_col: int = Field(ge=0)
    workspace: str | None = None
    git_remote: str | None = None
    git_commit: str | None = None
    labels: LabelList = Field(default_factory=list, max_length=20)


class SourceUpgradeIn(BaseModel):
    source_uri: str = Field(min_length=1)
    source_title: str | None = None


class LabelIn(BaseModel):
    label: str = Field(min_length=1, max_length=200)
    surface: str = Field(default="hud", pattern="^(browser|vscode|hud)$")


class OpenIn(BaseModel):
    """Deep-link dispatch (V17): exactly one of ``url`` / ``reveal``."""

    url: str | None = None
    reveal: str | None = None


class TabContextIn(BaseModel):
    """Ephemeral active-tab push (ADR 0004) — never persisted."""

    url: str = Field(min_length=1, max_length=4096)
    title: str | None = Field(default=None, max_length=1024)


class WorkspaceContextIn(BaseModel):
    """Ephemeral focused-workspace push (ADR 0004) — never persisted."""

    root: str | None = Field(default=None, max_length=4096)
    file: str | None = Field(default=None, max_length=4096)
