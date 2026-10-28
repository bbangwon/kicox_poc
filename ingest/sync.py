"""PostgreSQL → Qdrant 동기화 (§6.5), 현행 판 전환 (§7.3), 점검 (§7.5).

별도 장부 없이 원본 행의 indexed_hash와 현재 입력 해시가 다른 포인트만 다시 임베딩한다.
"""

import logging
from collections import defaultdict
from dataclasses import dataclass, field

import psycopg
from qdrant_client import QdrantClient, models

from ingest import config, sparse
from ingest.embed import OpenAIEmbedder
from ingest.points import Point, build

log = logging.getLogger(__name__)
COLLECTIONS = (config.CATALOG, config.CHUNKS)
UPSERT_BATCH = 64


class CollectionMismatch(Exception):
    pass


class AvgdlNotSet(Exception):
    pass


def client() -> QdrantClient:
    return QdrantClient(url=config.QDRANT_URL)


def _doc_filter(doc_id: str) -> models.Filter:
    return models.Filter(must=[models.FieldCondition(key="doc_id", match=models.MatchValue(value=doc_id))])


# ── 컬렉션 ──────────────────────────────────────────────────────────


def ensure_collections(qc: QdrantClient) -> None:
    """별칭 대상 컬렉션이 없으면 만들고, 있으면 벡터 설정이 현재 설정과 같은지 확인한다."""
    aliases = {a.alias_name: a.collection_name for a in qc.get_aliases().aliases}
    for alias in COLLECTIONS:
        name = config.physical_name(alias)
        if alias in aliases and aliases[alias] != name:
            raise CollectionMismatch(f"{alias} → {aliases[alias]} (현재 설정은 {name}). 재색인 · 별칭 전환이 필요합니다")
        if not qc.collection_exists(name):
            qc.create_collection(
                name,
                vectors_config={"dense": models.VectorParams(size=config.EMBED_DIM, distance=models.Distance.COSINE)}
                if config.EMBED_BASE_URL
                else {},
                sparse_vectors_config={"bm25": models.SparseVectorParams(modifier=models.Modifier.IDF)},
            )
            for key, schema in [
                ("kind", models.PayloadSchemaType.KEYWORD),
                ("doc_id", models.PayloadSchemaType.KEYWORD),
                ("is_current", models.PayloadSchemaType.BOOL),
            ] + (
                [("section_id", models.PayloadSchemaType.KEYWORD), ("verified", models.PayloadSchemaType.BOOL)]
                if alias == config.CHUNKS
                else []
            ):
                qc.create_payload_index(name, key, schema)
            log.info("컬렉션 생성: %s", name)
        _check_params(qc, name)
        if alias not in aliases:
            qc.update_collection_aliases(
                [models.CreateAliasOperation(create_alias=models.CreateAlias(collection_name=name, alias_name=alias))]
            )
            log.info("별칭 생성: %s → %s", alias, name)


def _check_params(qc: QdrantClient, name: str) -> None:
    params = qc.get_collection(name).config.params
    dense = (params.vectors or {}).get("dense") if isinstance(params.vectors, dict) else None
    bm25 = (params.sparse_vectors or {}).get("bm25")
    want_dense = config.EMBED_DIM if config.EMBED_BASE_URL else None
    got_dense = dense.size if dense else None
    if got_dense != want_dense or not bm25 or bm25.modifier != models.Modifier.IDF:
        raise CollectionMismatch(f"{name}: dense={got_dense} (기대 {want_dense}), bm25={bm25}")


# ── avgdl ───────────────────────────────────────────────────────────


def measure_avgdl(conn: psycopg.Connection) -> dict[str, float]:
    """검색 대상이 될 판(LOADED · INDEXED)의 포인트별 BM25 토큰 수 평균. child는 원문만 센다."""
    lengths: dict[str, list[int]] = defaultdict(list)
    for (doc_id,) in conn.execute("SELECT id FROM documents WHERE status IN ('LOADED', 'INDEXED')"):
        for p in build(conn, doc_id, {}):
            lengths[p.collection].append(len(sparse.tokens(p.sparse)))
    return {c: round(sum(v) / len(v), 1) for c, v in lengths.items() if v}


def configured_avgdl(conn: psycopg.Connection) -> dict[str, float]:
    got = {config.CATALOG: config.BM25_AVGDL_CATALOG, config.CHUNKS: config.BM25_AVGDL_CHUNKS}
    if all(got.values()):
        return got
    m = measure_avgdl(conn)
    raise AvgdlNotSet(
        "avgdl이 설정되지 않았습니다. 실측값을 .env에 고정하세요 (바꾸면 전체 재색인):\n"
        f"RAG_BM25_AVGDL_CATALOG={m.get(config.CATALOG, 0)}\nRAG_BM25_AVGDL_CHUNKS={m.get(config.CHUNKS, 0)}"
    )


# ── 동기화 ──────────────────────────────────────────────────────────


@dataclass
class SyncStats:
    doc_id: str
    expected: dict[str, int] = field(default_factory=dict)
    embedded: int = 0
    deleted: int = 0


def _stored_hashes(conn: psycopg.Connection, doc_id: str) -> dict[tuple[str, object], str | None]:
    stored: dict[tuple[str, object], str | None] = {}
    for table in ("documents", "sections", "chunks"):
        col = "id" if table == "documents" else "document_id"
        for rid, h in conn.execute(f"SELECT id, indexed_hash FROM {table} WHERE {col} = %s", (doc_id,)):
            stored[(table, rid)] = h
    return stored


def _to_struct(p: Point, dense: list[float] | None, avgdl: dict[str, float]) -> models.PointStruct:
    vector: dict = {"bm25": sparse.document(p.sparse, avgdl[p.collection])}
    if dense is not None:
        vector["dense"] = dense
    return models.PointStruct(id=str(p.id), vector=vector, payload=p.payload)


def sync_doc(
    conn: psycopg.Connection, qc: QdrantClient, embedder: OpenAIEmbedder | None, doc_id: str, avgdl: dict[str, float]
) -> SyncStats:
    points = build(conn, doc_id, avgdl)
    stats = SyncStats(doc_id, {c: sum(p.collection == c for p in points) for c in COLLECTIONS})
    stored = _stored_hashes(conn, doc_id)
    changed = [p for p in points if stored.get(p.row) != p.hash]

    # 1. 입력이 바뀐 포인트만 임베딩 → upsert → indexed_hash 갱신 (배치마다 커밋해 실패 시 이어서 함)
    #    catalog는 판 단위 묶음이라 documents.indexed_hash는 catalog 포인트를 모두 넣은 뒤에 적는다
    for i in range(0, len(changed), UPSERT_BATCH):
        batch = changed[i : i + UPSERT_BATCH]
        dense = embedder.embed([p.dense for p in batch]) if embedder else [None] * len(batch)
        by_collection: dict[str, list[models.PointStruct]] = defaultdict(list)
        for p, v in zip(batch, dense):
            by_collection[p.collection].append(_to_struct(p, v, avgdl))
        for alias, structs in by_collection.items():
            qc.upsert(alias, structs, wait=True)
        with conn.transaction():
            for p in batch:
                if p.row[0] != "documents":
                    conn.execute(f"UPDATE {p.row[0]} SET indexed_hash = %s WHERE id = %s", (p.hash, p.row[1]))
        stats.embedded += len(batch)
    catalog_hash = next(p.hash for p in points if p.row[0] == "documents")
    if stored.get(("documents", doc_id)) != catalog_hash:
        with conn.transaction():
            conn.execute("UPDATE documents SET indexed_hash = %s WHERE id = %s", (catalog_hash, doc_id))

    # 2. 입력이 같은 포인트는 payload만 덮어쓴다 (is_current · verified, 재청킹으로 바뀐 parent의 section_id)
    #    컬렉션마다 요청 하나로 묶는다
    unchanged = defaultdict(list)
    for p in points:
        if stored.get(p.row) == p.hash:
            unchanged[p.collection].append(
                models.OverwritePayloadOperation(overwrite_payload=models.SetPayload(payload=p.payload, points=[str(p.id)]))
            )
    for alias, ops in unchanged.items():
        qc.batch_update_points(alias, ops, wait=True)

    # 3. 있어야 할 목록에 없는 포인트(재청킹으로 사라진 청크 등)를 지운다
    for alias in COLLECTIONS:
        want = {str(p.id) for p in points if p.collection == alias}
        orphans = [pid for pid in _point_ids(qc, alias, doc_id) if pid not in want]
        if orphans:
            qc.delete(alias, points_selector=models.PointIdsList(points=orphans), wait=True)
            stats.deleted += len(orphans)

    with conn.transaction():
        conn.execute("UPDATE documents SET status = 'INDEXED' WHERE id = %s AND status = 'LOADED'", (doc_id,))
    return stats


def _point_ids(qc: QdrantClient, alias: str, doc_id: str) -> list[str]:
    ids, offset = [], None
    while True:
        records, offset = qc.scroll(
            alias, scroll_filter=_doc_filter(doc_id), limit=256, offset=offset, with_payload=False, with_vectors=False
        )
        ids += [str(r.id) for r in records]
        if offset is None:
            return ids


# ── 현행 판 전환 ────────────────────────────────────────────────────


def switch_current(conn: psycopg.Connection, qc: QdrantClient, regulation_key: str) -> tuple[str, list[str]] | None:
    """INDEXED 판 중 제개정일이 가장 늦은 판을 현행으로. 나머지 INDEXED 판은 SUPERSEDED + 포인트 삭제.

    PostgreSQL 커밋과 Qdrant 갱신 사이에는 두 판이 함께 검색될 수 있지만 검색 결과를 PostgreSQL로 다시 거른다.
    """
    with conn.transaction():
        row = conn.execute(
            "SELECT id FROM documents WHERE regulation_key = %s AND status = 'INDEXED'"
            " ORDER BY amended_on DESC NULLS LAST, is_current DESC LIMIT 1",
            (regulation_key,),
        ).fetchone()
        if not row:
            return None
        current = row[0]
        superseded = [
            r[0]
            for r in conn.execute(
                "UPDATE documents SET status = 'SUPERSEDED', is_current = false"
                " WHERE regulation_key = %s AND status = 'INDEXED' AND id <> %s RETURNING id",
                (regulation_key, current),
            )
        ]
        conn.execute("UPDATE documents SET is_current = true WHERE id = %s", (current,))
        # 이전 판 포인트는 지우므로 색인 해시도 비운다 (다시 살리면 전량 색인)
        conn.execute("UPDATE documents SET indexed_hash = NULL WHERE id = ANY(%s)", (superseded,))
        for table in ("sections", "chunks"):
            conn.execute(f"UPDATE {table} SET indexed_hash = NULL WHERE document_id = ANY(%s)", (superseded,))
    for alias in COLLECTIONS:
        qc.set_payload(alias, {"is_current": True}, points=_doc_filter(current), wait=True)
        for old in superseded:
            qc.delete(alias, points_selector=models.FilterSelector(filter=_doc_filter(old)), wait=True)
    return current, superseded


# ── 점검 ────────────────────────────────────────────────────────────


def check(conn: psycopg.Connection, qc: QdrantClient, avgdl: dict[str, float]) -> list[str]:
    """§7.5 점검. 문제 목록을 돌려준다 (비어 있으면 정상)."""
    problems = []
    for doc_id, status in conn.execute("SELECT id, status FROM documents ORDER BY id").fetchall():
        if status == "LOADED":
            continue  # 색인 전이거나 색인 실패. 다음 sync가 이어서 함
        # SUPERSEDED · REPEALED는 포인트가 없어야 함
        expected = {c: 0 for c in COLLECTIONS}
        if status == "INDEXED":
            for p in build(conn, doc_id, avgdl):
                expected[p.collection] += 1
        for alias in COLLECTIONS:
            got = qc.count(alias, count_filter=_doc_filter(doc_id), exact=True).count
            if got != expected[alias]:
                problems.append(f"{doc_id} {alias}: Qdrant {got} != PostgreSQL {expected[alias]}")
        if status == "INDEXED":
            (missing,) = conn.execute(
                "SELECT (SELECT count(*) FROM chunks WHERE document_id = %(d)s AND searchable AND indexed_hash IS NULL)"
                " + (SELECT count(*) FROM sections WHERE document_id = %(d)s AND searchable AND covers IS NOT NULL"
                "    AND indexed_hash IS NULL)",
                {"d": doc_id},
            ).fetchone()
            if missing:
                problems.append(f"{doc_id}: indexed_hash 빈 searchable 행 {missing}개")
    for key, n in conn.execute(
        "SELECT regulation_key, count(*) FILTER (WHERE is_current) FROM documents"
        " WHERE status = 'INDEXED' GROUP BY regulation_key HAVING count(*) FILTER (WHERE is_current) <> 1"
    ):
        problems.append(f"{key}: 현행 판 {n}개")
    return problems
