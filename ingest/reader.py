"""data/docs/{doc_id}/ 산출물 읽기. 전처리 코드는 import하지 않는다 (§3 원칙 3)."""

import hashlib
import json
from pathlib import Path

from pydantic import ValidationError

from ingest import config
from ingest.models import Artifacts

REQUIRED = ("meta.json", "chunks.json", "summaries.json")
OPTIONAL = ("tools.json", "parse.json", "verify.json")
# 묶음 해시에 넣는 파일. parse.json은 관문에만 쓰여 행에 영향이 없다
HASHED = ("meta.json", "chunks.json", "summaries.json", "tools.json", "verify.json")


class Rejected(Exception):
    """산출물을 읽을 수 없어 관문에서 탈락 (사유 목록)."""

    def __init__(self, reasons: list[str]):
        super().__init__("; ".join(reasons))
        self.reasons = reasons


def doc_dirs(src: Path, only: list[str] | None = None) -> list[Path]:
    if only:
        return [src / d for d in only]
    return sorted(p for p in src.iterdir() if p.is_dir())


def read(path: Path) -> Artifacts:
    missing = [f for f in REQUIRED if not (path / f).is_file()]
    if missing:
        raise Rejected([f"필수 파일 없음: {', '.join(missing)}"])
    raw = {f: (path / f).read_bytes() for f in REQUIRED + OPTIONAL if (path / f).is_file()}
    data = {f.removesuffix(".json"): json.loads(b) for f, b in raw.items()}

    version = data["meta"].get("schema_version", 1)
    if version not in config.SUPPORTED_SCHEMA_VERSIONS:
        raise Rejected([f"지원하지 않는 형식 버전: {version}"])

    h = hashlib.sha256(f"load:{config.LOAD_VERSION}".encode())
    for f in HASHED:
        h.update(f"\0{f}\0".encode() + raw.get(f, b""))
    try:
        return Artifacts(**data, content_hash=h.hexdigest()[:16])
    except ValidationError as e:
        reasons = [f"파일 형식 오류: {'.'.join(map(str, err['loc']))} {err['msg']}" for err in e.errors()[:5]]
        raise Rejected(reasons) from None
