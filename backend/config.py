"""환경 설정. 모든 값은 환경변수로 덮어쓸 수 있다."""

import os

OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://192.168.0.58:11434")
MODEL = os.getenv("LLM_MODEL", "qwen3.6:35b")
# Ollama 기본 num_ctx는 작아서 도구 스키마·대화 이력이 잘릴 수 있으므로 명시한다.
NUM_CTX = int(os.getenv("LLM_NUM_CTX", "32768"))
TEMPERATURE = float(os.getenv("LLM_TEMPERATURE", "0.3"))
# LLM 서버의 일시적 오류(5xx, CUDA 오류, 연결 끊김) 시 재시도 횟수와 기본 대기 시간(초, 시도마다 증가)
LLM_MAX_RETRIES = int(os.getenv("LLM_MAX_RETRIES", "2"))
LLM_RETRY_DELAY = float(os.getenv("LLM_RETRY_DELAY", "2.0"))

MCP_SERVERS = {
    "docs": {"url": os.getenv("MCP_DOCS_URL", "http://127.0.0.1:8101/mcp"), "transport": "streamable_http"},
    "ops": {"url": os.getenv("MCP_OPS_URL", "http://127.0.0.1:8102/mcp"), "transport": "streamable_http"},
}
