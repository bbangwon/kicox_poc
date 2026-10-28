"""PostgreSQL 적재 (§7.2). 문서 판 1건 = 트랜잭션 1개, 같은 입력으로 다시 실행하면 변경 없음."""

import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path

import psycopg
from psycopg.types.json import Jsonb

from ingest import config, gate, ids
from ingest.models import Artifacts

log = logging.getLogger(__name__)


@dataclass
class Result:
    doc_id: str
    action: str  # loaded | skipped
    counts: dict[str, int] = field(default_factory=dict)


def regulation_key(a: Artifacts) -> str:
    return f"alio:{a.meta.rule.idx}" if a.meta.rule else f"manual:{a.meta.doc_id}"


def counts(a: Artifacts) -> dict[str, int]:
    return {"sections": len(a.chunks.parents), "chunks": len(a.chunks.children), "tools": len(gate.approved_tools(a))}


def load(conn: psycopg.Connection, a: Artifacts) -> Result:
    """관문을 통과한 산출물을 적재한다. 묶음 해시가 같으면 건너뛴다."""
    doc_id = a.meta.doc_id
    with conn.transaction():
        row = conn.execute("SELECT content_hash FROM documents WHERE id = %s FOR UPDATE", (doc_id,)).fetchone()
        if row and row[0] == a.content_hash:
            return Result(doc_id, "skipped")
        _upsert(conn, a)
        got = conn.execute(
            "SELECT (SELECT count(*) FROM sections WHERE document_id = %(d)s),"
            " (SELECT count(*) FROM chunks WHERE document_id = %(d)s),"
            " (SELECT count(*) FROM tools WHERE document_id = %(d)s)",
            {"d": doc_id},
        ).fetchone()
        expected = counts(a)
        if dict(zip(expected, got)) != expected:  # §7.5 산출물 개수 = 행 수
            raise RuntimeError(f"{doc_id}: 행 수 불일치 {dict(zip(expected, got))} != {expected}")
    return Result(doc_id, "loaded", expected)


def _upsert(conn: psycopg.Connection, a: Artifacts) -> None:
    doc_id = a.meta.doc_id
    doc = a.summaries.doc.fields
    # 새 판은 LOADED · 현행 아님. 이미 있는 판은 상태 · 현행 여부 · 색인 해시를 그대로 둔다
    conn.execute(
        """
        INSERT INTO documents (id, regulation_key, name, amended_on, summary, not_for, keywords, questions, content_hash)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (id) DO UPDATE SET
            regulation_key = EXCLUDED.regulation_key, name = EXCLUDED.name, amended_on = EXCLUDED.amended_on,
            summary = EXCLUDED.summary, not_for = EXCLUDED.not_for, keywords = EXCLUDED.keywords,
            questions = EXCLUDED.questions, content_hash = EXCLUDED.content_hash
        """,
        (
            doc_id, regulation_key(a), a.meta.name, a.meta.rule.amended_on if a.meta.rule else None,
            doc.summary, doc.not_for or None, doc.keywords, doc.questions, a.content_hash,
        ),
    )  # fmt: skip

    parents = a.chunks.parents
    section_ids = dict(zip((p.id for p in parents), ids.content_ids(doc_id, "parent", [p.hash for p in parents])))
    for ordinal, p in enumerate(parents):
        s = a.summaries.parents.get(p.id)
        f = s.fields if s and s.parent_hash == p.hash and s.fields else None
        conn.execute(
            """
            INSERT INTO sections (id, document_id, ordinal, title, page_start, page_end, covers, not_covers,
                                  keywords, questions, searchable, content_hash)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (id) DO UPDATE SET
                ordinal = EXCLUDED.ordinal, title = EXCLUDED.title, page_start = EXCLUDED.page_start,
                page_end = EXCLUDED.page_end, covers = EXCLUDED.covers, not_covers = EXCLUDED.not_covers,
                keywords = EXCLUDED.keywords, questions = EXCLUDED.questions, searchable = EXCLUDED.searchable
            """,
            (
                section_ids[p.id], doc_id, ordinal, p.title, p.page_start, p.page_end,
                f.covers if f else None, f.not_covers if f else None,
                f.keywords if f else [], f.questions if f else [], p.searchable, p.hash,
            ),
        )  # fmt: skip

    children = a.chunks.children
    chunk_ids = ids.content_ids(doc_id, "child", [c.hash for c in children])
    for ordinal, (cid, c) in enumerate(zip(chunk_ids, children)):
        conn.execute(
            """
            INSERT INTO chunks (id, document_id, section_id, ordinal, header, body, tokens, page_start, page_end,
                                searchable, has_numbers, verified, content_hash)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (id) DO UPDATE SET
                section_id = EXCLUDED.section_id, ordinal = EXCLUDED.ordinal, tokens = EXCLUDED.tokens,
                page_start = EXCLUDED.page_start, page_end = EXCLUDED.page_end, searchable = EXCLUDED.searchable,
                has_numbers = EXCLUDED.has_numbers, verified = EXCLUDED.verified
            """,
            (
                cid, doc_id, section_ids[c.parent_id], ordinal, c.header, c.text, c.tokens, c.page_start, c.page_end,
                c.searchable, c.has_numbers, gate.is_verified(a, c.hash), c.hash,
            ),
        )  # fmt: skip
    conn.execute("DELETE FROM chunks WHERE document_id = %s AND id <> ALL(%s)", (doc_id, chunk_ids))

    # 승인이 풀린 도구를 먼저 지워야 같은 이름의 새 spec이 UNIQUE(document_id, name)에 걸리지 않는다
    tools = gate.approved_tools(a)
    tool_ids = [ids.tool_id(doc_id, t.spec_hash) for t in tools]
    conn.execute("DELETE FROM tools WHERE document_id = %s AND id <> ALL(%s)", (doc_id, tool_ids))
    for tid, t in zip(tool_ids, tools):
        conn.execute(
            """
            INSERT INTO tools (id, document_id, section_id, name, spec) VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (id) DO UPDATE SET section_id = EXCLUDED.section_id
            """,
            (tid, doc_id, section_ids[t.parent_id], t.spec["name"], Jsonb(t.spec)),
        )
        conn.execute(
            "INSERT INTO document_tools (document_id, tool_id, added_by) VALUES (%s, %s, 'ingest') ON CONFLICT DO NOTHING",
            (doc_id, tid),
        )
    conn.execute("DELETE FROM sections WHERE document_id = %s AND id <> ALL(%s)", (doc_id, list(section_ids.values())))


def copy_files(src: Path, doc_id: str) -> None:
    """원본 PDF · 쪽 이미지를 storage/docs/{doc_id}/로 복사. doc_id가 PDF 해시라 이미 있으면 같은 파일이다."""
    dest = config.STORAGE_DIR / doc_id
    files = [src / "source.pdf", *sorted((src / "pages").glob("*.png"))]
    for f in files:
        if not f.is_file():
            continue
        target = dest / f.relative_to(src)
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, target)
