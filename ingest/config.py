"""적재 설정. 모든 값은 환경변수로 덮어쓸 수 있다.

AI 서버 주소는 worker-ai-endpoints.env, DB 접속 정보는 .env (둘 다 .gitignore 대상)에서 읽고
이미 설정된 환경변수가 우선한다.
"""

import os
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

for _name in ("worker-ai-endpoints.env", ".env"):
    _file = ROOT / _name
    if _file.is_file():
        for _line in _file.read_text(encoding="utf-8").splitlines():
            _key, _sep, _value = _line.partition("=")
            if _sep and not _line.lstrip().startswith("#"):
                os.environ.setdefault(_key.strip(), _value.strip())

# PostgreSQL: 로컬 공용 컨테이너의 kicox DB. 없으면 migrate가 만든다
DATABASE_URL = os.getenv("KICOX_DATABASE_URL", "postgresql://localhost:5432/kicox")
QDRANT_URL = os.getenv("QDRANT_URL", "http://localhost:6333")

# 전처리 산출물 위치 (kicox_preprocess_poc/data/docs)와 원본 PDF · 쪽 이미지 복사 위치
SRC_DIR = Path(os.getenv("INGEST_SRC", ROOT.parent / "kicox_preprocess_poc" / "data" / "docs"))
STORAGE_DIR = Path(os.getenv("STORAGE_DIR", ROOT / "storage" / "docs"))

# 로더가 읽을 수 있는 산출물 형식 버전 (§2.3). meta.json에 없으면 1
SUPPORTED_SCHEMA_VERSIONS = {1}
# 로더 코드가 행을 만드는 방식이 바뀌면 올린다 (content_hash에 들어가 같은 산출물도 다시 적재됨)
LOAD_VERSION = 1

# 이 PoC 전용 uuid5 네임스페이스 (§5). 바꾸면 모든 ID가 바뀐다
NS = uuid.UUID("c21cf703-e657-41f2-ba2d-ed2b8e117b72")

# 임베딩 (§9). EMBED_BASE_URL이 없으면 dense 없이 BM25만 색인한다
EMBED_BASE_URL = os.getenv("EMBED_BASE_URL", "").rstrip("/")
EMBED_MODEL = os.getenv("EMBED_MODEL", "bge-m3-ko")
EMBED_DIM = int(os.getenv("EMBED_DIM", "1024"))
EMBED_BATCH_SIZE = int(os.getenv("EMBED_BATCH_SIZE", "32"))
EMBED_MAX_CONCURRENCY = int(os.getenv("EMBED_MAX_CONCURRENCY", "2"))
EMBED_TIMEOUT_SECONDS = float(os.getenv("EMBED_TIMEOUT_SECONDS", "60"))
EMBED_MAX_TOKENS = int(os.getenv("EMBED_MAX_TOKENS", "8192"))
EMBED_MAX_RETRIES = int(os.getenv("EMBED_MAX_RETRIES", "3"))
# bge-m3는 질의 · 문서 접두어 없음
EMBED_QUERY_PREFIX = os.getenv("EMBED_QUERY_PREFIX", "")
EMBED_DOCUMENT_PREFIX = os.getenv("EMBED_DOCUMENT_PREFIX", "")

# 임베딩 입력 형식(§6.2) 버전. 바꾸면 입력 해시가 모두 달라져 전체 재색인된다
EMBED_TEXT_VERSION = 1

# BM25 (§9). avgdl은 컬렉션별 실측값을 고정해 쓴다. 비어 있으면 sync가 실측값을 알려 주고 멈춘다
BM25_K1 = 1.2
BM25_B = 0.75
BM25_AVGDL_CATALOG = float(os.getenv("RAG_BM25_AVGDL_CATALOG") or 0) or None
BM25_AVGDL_CHUNKS = float(os.getenv("RAG_BM25_AVGDL_CHUNKS") or 0) or None

# Qdrant 컬렉션 별칭 (§6.1)
CATALOG = "reg_catalog"
CHUNKS = "reg_chunks"


def physical_name(alias: str) -> str:
    """{별칭}__{모델}__{차원}. 임베딩이 없으면 {별칭}__bm25__0."""
    return f"{alias}__{EMBED_MODEL}__{EMBED_DIM}" if EMBED_BASE_URL else f"{alias}__bm25__0"
