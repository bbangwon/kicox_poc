"""PostgreSQL 행에서 "있어야 할 포인트"를 만든다 (§6.2 임베딩 입력, §6.3 payload).

Qdrant는 PostgreSQL만으로 다시 만들 수 있어야 하므로(§3 원칙 2) 산출물 파일은 읽지 않는다.
"""

import hashlib
import uuid
from dataclasses import dataclass

import psycopg

from ingest import config, ids
from ingest.gate import compact_header


@dataclass
class Point:
    collection: str  # 별칭 (reg_catalog · reg_chunks)
    id: uuid.UUID
    kind: str
    dense: str  # dense 입력
    sparse: str  # BM25 입력
    payload: dict
    row: tuple[str, object]  # indexed_hash를 적는 원본 행 (테이블, id)
    hash: str = ""  # 입력 해시. catalog는 판 하나의 묶음 해시


def _input_hash(collection: str, avgdl: float, *parts: str) -> str:
    """입력 형식 버전 · 물리 컬렉션 · avgdl이 바뀌어도 다시 색인되도록 함께 넣는다."""
    h = hashlib.sha256(f"{config.EMBED_TEXT_VERSION}\0{config.physical_name(collection)}\0{avgdl}".encode())
    for p in parts:
        h.update(b"\0" + p.encode())
    return h.hexdigest()[:16]


def build(conn: psycopg.Connection, doc_id: str, avgdl: dict[str, float]) -> list[Point]:
    """avgdl: 별칭 → 값. 해시 계산 전 단계(avgdl 실측)에서는 빈 dict를 넘기고 hash를 쓰지 않는다."""
    name, summary, keywords, questions, is_current = conn.execute(
        "SELECT name, summary, keywords, questions, is_current FROM documents WHERE id = %s", (doc_id,)
    ).fetchone()
    base = {"doc_id": doc_id, "is_current": is_current}
    points: list[Point] = []

    # reg_catalog: 문서 요약 1 + 예상 질문 N. 판 하나를 묶음으로 색인한다
    doc_text = f"{name}\n{summary}\n키워드: {', '.join(keywords)}"
    catalog = [
        Point(
            config.CATALOG, ids.doc_summary_id(doc_id), "doc_summary", doc_text, doc_text,
            {**base, "kind": "doc_summary"}, ("documents", doc_id),
        )
    ] + [
        Point(
            config.CATALOG, ids.doc_question_id(doc_id, i), "doc_question", q, q,
            {**base, "kind": "doc_question"}, ("documents", doc_id),
        )
        for i, q in enumerate(questions)
    ]  # fmt: skip
    group = _input_hash(config.CATALOG, avgdl.get(config.CATALOG, 0), *(p.dense for p in catalog))
    for p in catalog:
        p.hash = group
    points += catalog

    # reg_chunks: searchable parent 요약. not_covers는 넣지 않는다
    for sid, title, covers, kw, qs in conn.execute(
        "SELECT id, title, covers, keywords, questions FROM sections"
        " WHERE document_id = %s AND searchable AND covers IS NOT NULL ORDER BY ordinal",
        (doc_id,),
    ):
        text = f"[{name} > {title}]\n{covers}\n키워드: {', '.join(kw)}"
        if qs:
            text += f"\n예상 질문: {' / '.join(qs)}"
        points.append(
            Point(
                config.CHUNKS, ids.parent_summary_id(sid), "parent_summary", text, text,
                {**base, "kind": "parent_summary", "section_id": str(sid)}, ("sections", sid),
            )
        )  # fmt: skip

    # reg_chunks: searchable child. dense는 계층정보 + 원문, sparse는 원문만
    for cid, sid, header, body, verified in conn.execute(
        "SELECT id, section_id, header, body, verified FROM chunks WHERE document_id = %s AND searchable ORDER BY ordinal",
        (doc_id,),
    ):
        dense = f"[{compact_header(header)}]\n{body}"
        points.append(
            Point(
                config.CHUNKS, cid, "child", dense, body,
                {**base, "kind": "child", "section_id": str(sid), "verified": verified, "text": body, "dense_text": dense},
                ("chunks", cid),
            )
        )  # fmt: skip

    for p in points:
        if p.collection == config.CHUNKS:
            p.hash = _input_hash(p.collection, avgdl.get(p.collection, 0), p.dense, p.sparse)
    return points
