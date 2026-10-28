"""ID 규칙 (§5). 내용 해시 기반이라 재청킹해도 내용이 같은 청크는 ID가 유지된다."""

import uuid
from collections import Counter

from ingest.config import NS


def _id(key: str) -> uuid.UUID:
    return uuid.uuid5(NS, key)


def content_ids(doc_id: str, kind: str, hashes: list[str]) -> list[uuid.UUID]:
    """같은 문서 안에서 해시가 반복되면 n을 0, 1, 2 …로 늘린다 (보통 0)."""
    seen: Counter[str] = Counter()
    ids = []
    for h in hashes:
        ids.append(_id(f"{doc_id}:{kind}:{h}:{seen[h]}"))
        seen[h] += 1
    return ids


def tool_id(doc_id: str, spec_hash: str) -> uuid.UUID:
    return _id(f"{doc_id}:tool:{spec_hash}")


def doc_summary_id(doc_id: str) -> uuid.UUID:
    return _id(f"{doc_id}:doc_summary")


def doc_question_id(doc_id: str, i: int) -> uuid.UUID:
    return _id(f"{doc_id}:doc_question:{i}")


def parent_summary_id(section_id: uuid.UUID) -> uuid.UUID:
    return _id(f"{section_id}:summary")
