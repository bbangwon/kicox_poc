"""전처리 산출물 형식 버전 1 (kicox_preprocess_poc c97fd03)의 pydantic 모델.

로더가 읽는 필드만 정의한다. 필드 추가는 형식 버전을 올리지 않으므로(§2.3) 모르는 필드는 무시한다.
"""

from pydantic import BaseModel, ConfigDict, Field


class _Model(BaseModel):
    model_config = ConfigDict(extra="ignore")


class Rule(_Model):
    idx: str
    amended_on: str | None = Field(None, alias="제개정일")


class Meta(_Model):
    schema_version: int = 1
    doc_id: str
    name: str
    pages: int
    rule: Rule | None = None


class Parent(_Model):
    id: str
    title: str
    child_ids: list[str]
    page_start: int | None = None
    page_end: int | None = None
    searchable: bool
    hash: str


class Child(_Model):
    id: str
    parent_id: str
    header: str
    text: str
    tokens: int
    page_start: int | None = None
    page_end: int | None = None
    has_numbers: bool
    searchable: bool
    hash: str


class Chunks(_Model):
    parents: list[Parent]
    children: list[Child]


class ParentSummaryFields(_Model):
    covers: str = ""
    not_covers: str = ""
    keywords: list[str] = []
    questions: list[str] = []


class ParentSummary(_Model):
    parent_id: str
    parent_hash: str
    fields: ParentSummaryFields | None = None
    error: str | None = None


class DocSummaryFields(_Model):
    summary: str = ""
    keywords: list[str] = []
    questions: list[str] = []
    not_for: str = ""


class DocSummary(_Model):
    fields: DocSummaryFields | None = None
    error: str | None = None


class Summaries(_Model):
    parents: dict[str, ParentSummary]
    doc: DocSummary | None = None


class ToolChecks(_Model):
    status: str


class ToolEntry(_Model):
    parent_id: str
    parent_hash: str
    spec: dict
    spec_hash: str
    checks: ToolChecks


class Tools(_Model):
    tools: list[ToolEntry] = []


class Parse(_Model):
    truncated: list = []


class VerifyMark(_Model):
    verified: bool = False


class Artifacts(_Model):
    """문서 판 하나의 산출물 묶음."""

    meta: Meta
    chunks: Chunks
    summaries: Summaries
    tools: Tools | None = None
    parse: Parse | None = None
    verify: dict[str, VerifyMark] = {}
    content_hash: str  # 산출물 원문 바이트로 계산 (reader)
