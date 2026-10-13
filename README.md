# DGX PoC - LLM 서버 연동 및 응용 프로젝트

로컬 인프라(DGX)에 호스팅된 Ollama 기반 LLM 서버와 연동하여 LLM 응용 소프트웨어를 개발하고 검증하는 PoC 프로젝트입니다.

## 문서 안내

* 📖 **[LLM 서버 사양 및 연동 가이드](docs/LLM_SERVER_SPEC.md)**:
  * 서버 엔드포인트 및 접속 정보 (`http://192.168.0.58:11434`)
  * 호스팅 모델 사양 비교 (`gpt-oss:latest`, `qwen3.6:35b`, `gemma4:31b`)
  * 벤치마크 및 테스트 결과 분석
  * Python (`openai` SDK, `httpx`, `LangChain`) 연동 예제 코드
  * 개발 시 고려사항 및 베스트 프랙티스

## Agent Orchestration Viewer (프로토타입)

`qwen3.6:35b` 기반 에이전트의 오케스트레이션 흐름과 MCP 도구 호출을 실시간으로 시각화하는 웹 UI입니다.

```
React (Vite, :5173) ──SSE──▶ FastAPI (:8000) ──▶ LangGraph [planner → agent ⇄ tools]
                                                     │             │
                                          ChatOllama(qwen3.6)   langchain-mcp-adapters
                                                                   ├─▶ FastMCP docs (:8101) search_internal_docs
                                                                   └─▶ FastMCP ops  (:8102) calculate, send_email
```

### 실행

```bash
uv sync
(cd web && npm install)
./scripts/dev.sh        # → http://localhost:5173
```

### 구성

| 경로 | 내용 |
| :--- | :--- |
| `mcp_servers/` | FastMCP 서버 2종 (mock 데이터, 이메일은 실제 발송하지 않음) |
| `backend/graph.py` | LangGraph 그래프. 각 노드가 `get_stream_writer()`로 시각화 이벤트를 발행 |
| `backend/app.py` | `GET /api/graph` (그래프 구조·MCP 도구 목록), `POST /api/chat` (실행 이벤트 SSE) |
| `web/` | 채팅 / React Flow 그래프 / 실행 타임라인 3분할 UI |

### SSE 이벤트

`run_start` · `node_start` / `node_end` · `plan` · `thinking` / `token` (스트리밍 델타) · `llm_end` (도구 호출 결정, 토큰 수, tok/s) · `tool_call` / `tool_result` (MCP 서버명, 인자, 결과, 소요 시간) · `error` · `run_end`

### 주의사항

* `langchain-mcp-adapters`가 `mcp<2`를 요구하므로 `fastmcp`는 3.x로 고정되어 있습니다 (fastmcp 4.x는 `mcp>=2`).
* Ollama 기본 `num_ctx`가 작아 `LLM_NUM_CTX=32768`을 명시합니다. 모델·서버 주소 등은 `backend/config.py`의 환경변수로 변경할 수 있습니다.
