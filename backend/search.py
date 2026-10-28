"""규정 검색 (설계 노트 §4.2, DB_LOADING_PLAN §6.4).

그래프 노드가 직접 부르는 함수 4개: search_docs · search_chunks · list_sections · get_chunks.
색인과 같은 임베딩 · BM25 함수(ingest.embed · ingest.sparse)를 써야 하므로 ingest 모듈을 그대로 쓴다.
Qdrant 결과는 그대로 믿지 않고 PostgreSQL에서 현행 판인지 다시 확인한다.
"""

import asyncio
import time
from collections import defaultdict
from dataclasses import dataclass, field
from functools import cache

from qdrant_client import QdrantClient, models

from ingest import config, db, sparse
from ingest.embed import EmbedKind, get_embedder
from ingest.sync import client

RRF_K = 60
CATALOG_TOPK = 40  # 질의 · 신호마다
DOC_CANDIDATES = 20
CHUNK_TOPK = {"child": 20, "parent_summary": 10}  # kind별로 따로 뽑는다 (DB_LOADING_PLAN §11-10)


@cache
def _qdrant() -> QdrantClient:
    return client()


@cache
def _embedder():
    return get_embedder()


def dense_enabled() -> bool:
    return _embedder() is not None


def rrf(rankings: dict[str, list[str]], k: int = RRF_K) -> tuple[dict[str, float], dict[str, dict[str, int]]]:
    """신호별 순위 목록(id 리스트)을 RRF로 합친다. → (id별 점수, id별 {신호: 1부터 순위})."""
    scores: dict[str, float] = defaultdict(float)
    ranks: dict[str, dict[str, int]] = defaultdict(dict)
    for signal, ids in rankings.items():
        for rank, pid in enumerate(ids, 1):
            scores[pid] += 1 / (k + rank)
            ranks[pid][signal] = rank
    return dict(scores), dict(ranks)


def group_best(scores: dict[str, float], key_of: dict[str, str]) -> dict[str, tuple[float, str]]:
    """포인트 점수를 key(doc_id · section_id)별 최고점으로 묶는다. → key별 (점수, 최고점 포인트)."""
    best: dict[str, tuple[float, str]] = {}
    for pid, s in scores.items():
        key = key_of[pid]
        if key not in best or s > best[key][0]:
            best[key] = (s, pid)
    return best


def _filter(doc_ids: list[str] | None = None, kind: str | None = None) -> models.Filter:
    must = [models.FieldCondition(key="is_current", match=models.MatchValue(value=True))]
    if doc_ids is not None:
        must.append(models.FieldCondition(key="doc_id", match=models.MatchAny(any=doc_ids)))
    if kind:
        must.append(models.FieldCondition(key="kind", match=models.MatchValue(value=kind)))
    return models.Filter(must=must)


def _query(collection: str, text: str, dense: list[float] | None, flt: models.Filter, limit: int) -> dict[str, list]:
    """신호별 결과 포인트 목록. dense가 None이면 BM25만."""
    qc = _qdrant()
    out = {
        "bm25": qc.query_points(
            collection, query=sparse.query(text), using="bm25", query_filter=flt, limit=limit, with_payload=True
        ).points
    }
    if dense is not None:
        out["dense"] = qc.query_points(
            collection, query=dense, using="dense", query_filter=flt, limit=limit, with_payload=True
        ).points
    return out


def _embed(texts: list[str]) -> list[list[float]] | list[None]:
    e = _embedder()
    return e.embed(texts, EmbedKind.QUERY) if e else [None] * len(texts)


# ── router: 문서 후보 ────────────────────────────────────────────────


@dataclass
class DocCandidate:
    doc_id: str
    name: str
    summary: str
    not_for: str | None
    score: float
    # 질의별 최고 순위 {"q1:dense": 3, "q1:bm25": 1, ...}
    ranks: dict[str, int] = field(default_factory=dict)
    hit: str = ""  # 최고점 포인트 종류 (doc_summary · doc_question)


def _search_docs(queries: list[str]) -> list[DocCandidate]:
    rankings: dict[str, list[str]] = {}
    doc_of: dict[str, str] = {}
    kind_of: dict[str, str] = {}
    for qi, (text, dense) in enumerate(zip(queries, _embed(queries)), 1):
        for signal, points in _query(config.CATALOG, text, dense, _filter(), CATALOG_TOPK).items():
            rankings[f"q{qi}:{signal}"] = [str(p.id) for p in points]
            for p in points:
                doc_of[str(p.id)] = p.payload["doc_id"]
                kind_of[str(p.id)] = p.payload["kind"]
    scores, ranks = rrf(rankings)
    best = group_best(scores, doc_of)
    doc_ranks: dict[str, dict[str, int]] = defaultdict(dict)
    for pid, r in ranks.items():
        d = doc_ranks[doc_of[pid]]
        for signal, rank in r.items():
            d[signal] = min(rank, d.get(signal, rank))

    order = sorted(best, key=lambda d: best[d][0], reverse=True)[:DOC_CANDIDATES]
    with db.connect() as conn:
        rows = {
            r[0]: r[1:]
            for r in conn.execute(
                "SELECT id, name, summary, not_for FROM documents"
                " WHERE id = ANY(%s) AND status = 'INDEXED' AND is_current",
                (order,),
            )
        }
    return [
        DocCandidate(d, *rows[d], score=round(best[d][0], 5), ranks=doc_ranks[d], hit=kind_of[best[d][1]])
        for d in order
        if d in rows
    ]


# ── planner: 절 · 청크 적중 ──────────────────────────────────────────


@dataclass
class Section:
    section_id: str
    doc_id: str
    doc_name: str
    ordinal: int
    title: str
    covers: str | None
    not_covers: str | None
    page_start: int | None
    page_end: int | None
    chunk_ids: list[str] = field(default_factory=list)


@dataclass
class SectionHit:
    section_id: str
    score: float
    summary_hit: bool  # parent 요약 포인트가 걸렸는지
    child_ids: list[str]  # 적중 child (점수 순)


def _search_chunks(query: str, doc_ids: list[str]) -> list[SectionHit]:
    (dense,) = _embed([query])
    rankings: dict[str, list[str]] = {}
    section_of: dict[str, str] = {}
    kind_of: dict[str, str] = {}
    for kind, limit in CHUNK_TOPK.items():
        for signal, points in _query(config.CHUNKS, query, dense, _filter(doc_ids, kind), limit).items():
            rankings[f"{kind}:{signal}"] = [str(p.id) for p in points]
            for p in points:
                section_of[str(p.id)] = p.payload["section_id"]
                kind_of[str(p.id)] = kind
    scores, _ = rrf(rankings)
    by_section: dict[str, list[str]] = defaultdict(list)
    for pid in sorted(scores, key=scores.get, reverse=True):
        by_section[section_of[pid]].append(pid)
    best = group_best(scores, section_of)
    return [
        SectionHit(
            sid,
            round(best[sid][0], 5),
            any(kind_of[p] == "parent_summary" for p in pids),
            [p for p in pids if kind_of[p] == "child"],
        )
        for sid, pids in sorted(by_section.items(), key=lambda kv: best[kv[0]][0], reverse=True)
    ]


def _list_sections(doc_ids: list[str]) -> list[Section]:
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT s.id, s.document_id, d.name, s.ordinal, s.title, s.covers, s.not_covers, s.page_start, s.page_end,"
            "       coalesce(array_agg(c.id ORDER BY c.ordinal) FILTER (WHERE c.id IS NOT NULL), '{}')"
            " FROM sections s JOIN documents d ON d.id = s.document_id"
            " LEFT JOIN chunks c ON c.section_id = s.id AND c.searchable"
            " WHERE s.document_id = ANY(%s) AND d.is_current"
            " GROUP BY s.id, d.name ORDER BY array_position(%s, s.document_id), s.ordinal",
            (doc_ids, doc_ids),
        ).fetchall()
    return [Section(str(r[0]), *r[1:9], chunk_ids=[str(c) for c in r[9]]) for r in rows]


# ── agent: 원문 ──────────────────────────────────────────────────────


@dataclass
class Chunk:
    chunk_id: str
    doc_id: str
    doc_name: str
    section_title: str
    ordinal: int
    header: str
    body: str
    page_start: int | None
    page_end: int | None
    has_numbers: bool
    verified: bool
    neighbor: bool  # 요청한 청크가 아니라 앞뒤 이웃으로 붙은 것


def _get_chunks(ids: list[str], neighbors: int = 1) -> list[Chunk]:
    """요청한 child와 같은 문서 ordinal 앞뒤 이웃. 문서 · ordinal 순으로 돌려준다."""
    if not ids:
        return []
    with db.connect() as conn:
        rows = conn.execute(
            "WITH req AS (SELECT document_id, ordinal FROM chunks WHERE id = ANY(%s::uuid[]))"
            " SELECT c.id, c.document_id, d.name, s.title, c.ordinal, c.header, c.body, c.page_start, c.page_end,"
            "        c.has_numbers, c.verified, c.id <> ALL(%s::uuid[])"
            " FROM chunks c JOIN documents d ON d.id = c.document_id JOIN sections s ON s.id = c.section_id"
            " WHERE d.is_current AND EXISTS ("
            "   SELECT 1 FROM req WHERE req.document_id = c.document_id AND abs(req.ordinal - c.ordinal) <= %s)"
            " ORDER BY c.document_id, c.ordinal",
            (ids, ids, neighbors),
        ).fetchall()
    return [Chunk(str(r[0]), *r[1:]) for r in rows]


# ── 비동기 진입점 (Qdrant · PostgreSQL · 임베딩 클라이언트가 동기라 스레드에서 돈다) ──


async def _timed(fn, *args):
    started = time.perf_counter()
    result = await asyncio.to_thread(fn, *args)
    return result, round((time.perf_counter() - started) * 1000)


async def search_docs(queries: list[str]) -> tuple[list[DocCandidate], int]:
    return await _timed(_search_docs, queries)


async def search_chunks(query: str, doc_ids: list[str]) -> tuple[list[SectionHit], int]:
    return await _timed(_search_chunks, query, doc_ids)


async def list_sections(doc_ids: list[str]) -> list[Section]:
    return await asyncio.to_thread(_list_sections, doc_ids)


async def get_chunks(ids: list[str], neighbors: int = 1) -> list[Chunk]:
    return await asyncio.to_thread(_get_chunks, ids, neighbors)


def _documents() -> list[dict]:
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT d.id, d.name, d.amended_on, count(c.id) FROM documents d LEFT JOIN chunks c ON c.document_id = d.id"
            " WHERE d.is_current AND d.status = 'INDEXED' GROUP BY d.id ORDER BY d.name"
        ).fetchall()
    return [{"doc_id": r[0], "name": r[1], "amended_on": r[2] and r[2].isoformat(), "chunks": r[3]} for r in rows]


async def documents() -> list[dict]:
    """현행 · 색인 완료 문서 목록 (화면 상단 · 참조문서 없음 안내용)."""
    return await asyncio.to_thread(_documents)

