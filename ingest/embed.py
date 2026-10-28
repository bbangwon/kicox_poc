"""임베딩 호출 (§9). 호출부는 get_embedder().embed(texts, kind)만 안다.

지금 공급자는 OpenAI 호환 /v1/embeddings (vLLM bge-m3-ko) 하나. 색인과 질의가 같은 함수를 쓴다.
"""

import logging
import time
from concurrent.futures import ThreadPoolExecutor
from enum import Enum

import httpx

from ingest import config

log = logging.getLogger(__name__)


class EmbedKind(Enum):
    QUERY = "query"
    DOCUMENT = "document"


class EmbedError(Exception):
    def __init__(self, message: str, retryable: bool):
        super().__init__(message)
        self.retryable = retryable


class OpenAIEmbedder:
    def __init__(self, base_url: str, model: str, dim: int):
        self.model = model
        self.dim = dim
        self.client = httpx.Client(base_url=base_url, timeout=config.EMBED_TIMEOUT_SECONDS)

    def embed(self, texts: list[str], kind: EmbedKind = EmbedKind.DOCUMENT) -> list[list[float]]:
        prefix = config.EMBED_QUERY_PREFIX if kind is EmbedKind.QUERY else config.EMBED_DOCUMENT_PREFIX
        batches = [
            [prefix + t for t in texts[i : i + config.EMBED_BATCH_SIZE]]
            for i in range(0, len(texts), config.EMBED_BATCH_SIZE)
        ]
        with ThreadPoolExecutor(config.EMBED_MAX_CONCURRENCY) as pool:
            return [v for vectors in pool.map(self._with_retry, batches) for v in vectors]

    def _with_retry(self, texts: list[str]) -> list[list[float]]:
        for attempt in range(config.EMBED_MAX_RETRIES + 1):
            try:
                return self._call(texts)
            except EmbedError as e:
                if not e.retryable or attempt == config.EMBED_MAX_RETRIES:
                    raise
                log.warning("임베딩 재시도 %d/%d: %s", attempt + 1, config.EMBED_MAX_RETRIES, e)
                time.sleep(2**attempt)
        raise AssertionError("unreachable")

    def _call(self, texts: list[str]) -> list[list[float]]:
        try:
            r = self.client.post(
                "/embeddings",
                json={"model": self.model, "input": texts, "truncate_prompt_tokens": config.EMBED_MAX_TOKENS},
            )
        except (httpx.TransportError, httpx.TimeoutException) as e:
            raise EmbedError(f"연결 실패: {e!r}", retryable=True) from e
        if r.status_code == 429 or r.status_code >= 500:
            raise EmbedError(f"HTTP {r.status_code}: {r.text[:200]}", retryable=True)
        if r.status_code >= 400:
            raise EmbedError(f"HTTP {r.status_code}: {r.text[:200]}", retryable=False)
        data = sorted(r.json()["data"], key=lambda d: d["index"])
        vectors = [d["embedding"] for d in data]
        # 섞인 벡터 금지: 개수나 차원이 하나라도 다르면 배치 전체 실패
        if len(vectors) != len(texts) or any(len(v) != self.dim for v in vectors):
            dims = sorted({len(v) for v in vectors})
            raise EmbedError(f"응답 불일치: {len(vectors)}/{len(texts)}개, 차원 {dims} (기대 {self.dim})", retryable=False)
        return vectors


def get_embedder() -> OpenAIEmbedder | None:
    """EMBED_BASE_URL이 없으면 None (BM25만 색인)."""
    if not config.EMBED_BASE_URL or not config.EMBED_MODEL:
        return None
    return OpenAIEmbedder(config.EMBED_BASE_URL, config.EMBED_MODEL, config.EMBED_DIM)
