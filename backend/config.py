"""환경 설정. 모든 값은 환경변수로 덮어쓸 수 있다."""

import os
from pathlib import Path

# AI 서버 주소는 인증이 없어 주소 자체가 접근권이므로 코드에 두지 않는다.
# 프로젝트 루트의 env 파일(.gitignore 대상)에서 읽고, 이미 설정된 환경변수가 우선한다.
_ENV_FILE = Path(os.getenv("AI_ENDPOINTS_FILE", Path(__file__).resolve().parent.parent / "worker-ai-endpoints.env"))
if _ENV_FILE.is_file():
    for _line in _ENV_FILE.read_text(encoding="utf-8").splitlines():
        _key, _sep, _value = _line.partition("=")
        if _sep and not _line.lstrip().startswith("#"):
            os.environ.setdefault(_key.strip(), _value.strip())

# vLLM OpenAI 호환 엔드포인트 (예: https://…/v1). 인증이 없어도 SDK에 api_key 문자열이 필요하다.
LLM_BASE_URL = os.getenv("LLM_BASE_URL")
LLM_API_KEY = os.getenv("LLM_API_KEY", "EMPTY")
MODEL = os.getenv("LLM_MODEL", "seed32-think")
TEMPERATURE = float(os.getenv("LLM_TEMPERATURE", "0.3"))
# 응답 하나의 최대 생성 토큰(Thinking 포함). 없으면 생각이 반복될 때 컨텍스트 끝(32K)까지 생성한다.
MAX_TOKENS = int(os.getenv("LLM_MAX_TOKENS", "4096"))
PLANNER_MAX_TOKENS = int(os.getenv("LLM_PLANNER_MAX_TOKENS", "512"))
# 요청 하나의 최대 대기 시간(초). 서버 측 요청 타임아웃은 600초다.
LLM_TIMEOUT = float(os.getenv("LLM_TIMEOUT", "300"))
# LLM 서버의 일시적 오류(5xx, 연결 끊김) 시 재시도 횟수와 기본 대기 시간(초, 시도마다 증가)
LLM_MAX_RETRIES = int(os.getenv("LLM_MAX_RETRIES", "2"))
LLM_RETRY_DELAY = float(os.getenv("LLM_RETRY_DELAY", "2.0"))
