"""BM25 sparse 벡터 (원 프로젝트 방식, ASSEMBLY_RAG_REFERENCE.md §2.3).

토큰 = 낱말 + 한글 낱말 안 문자 2-gram, 불용어 · 한 글자 제외, 인덱스 = CRC32(UTF-8).
문서 값은 BM25 TF 포화, 질의 값은 1.0, IDF는 Qdrant modifier=IDF가 곱한다.
색인과 질의가 같은 tokens()를 써야 하며, 규칙을 바꾸면 전체 재색인이 필요하다.
"""

import re
import zlib
from collections import Counter

from qdrant_client import models

from ingest import config

WORD_RE = re.compile(r"[0-9A-Za-z가-힣]+")
HANGUL_RE = re.compile(r"[가-힣]")
# 규정 본문 어디에나 나오는 낱말 (원 프로젝트 목록은 국회 요구자료 기준이라 새로 둠)
STOPWORDS = frozenset(
    "및 등 또는 그 이 해당 경우 관한 관하여 따른 따라 위한 의한 의하여 한다 하는 하여 된다 있다 없다 있는 없는 수 것 때 각".split()
)


def tokens(text: str) -> list[str]:
    out = []
    for word in WORD_RE.findall(text.lower()):
        if len(word) < 2 or word in STOPWORDS:
            continue
        out.append(word)
        if len(word) > 2 and HANGUL_RE.search(word):
            out.extend(g for g in (word[i : i + 2] for i in range(len(word) - 1)) if g not in STOPWORDS)
    return out


def _vector(weights: dict[int, float]) -> models.SparseVector:
    idx = sorted(weights)
    return models.SparseVector(indices=idx, values=[weights[i] for i in idx])


def document(text: str, avgdl: float) -> models.SparseVector:
    toks = tokens(text)
    dl = len(toks)
    k1, b = config.BM25_K1, config.BM25_B
    weights: dict[int, float] = {}
    for tok, tf in Counter(toks).items():
        i = zlib.crc32(tok.encode())
        # CRC32 충돌은 값을 더한다
        weights[i] = weights.get(i, 0.0) + tf * (k1 + 1) / (tf + k1 * (1 - b + b * dl / avgdl))
    return _vector(weights)


def query(text: str) -> models.SparseVector:
    return _vector({zlib.crc32(t.encode()): 1.0 for t in set(tokens(text))})
