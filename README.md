# DGX PoC - LLM 서버 연동 및 응용 프로젝트

로컬 인프라(DGX)에 호스팅된 Ollama 기반 LLM 서버와 연동하여 LLM 응용 소프트웨어를 개발하고 검증하는 PoC 프로젝트입니다.

## 문서 안내

* 📖 **[LLM 서버 사양 및 연동 가이드](docs/LLM_SERVER_SPEC.md)**:
  * 서버 엔드포인트 및 접속 정보 (`http://192.168.0.58:11434`)
  * 호스팅 모델 사양 비교 (`gpt-oss:latest`, `qwen3.6:35b`, `gemma4:31b`)
  * 벤치마크 및 테스트 결과 분석
  * Python (`openai` SDK, `httpx`, `LangChain`) 연동 예제 코드
  * 개발 시 고려사항 및 베스트 프랙티스
* 🧰 **[사내 규정 Q&A 에이전트 MCP 도구 설계](docs/MCP_TOOL_DESIGN.md)**:
  * 개인교육훈련비·여비 규정 PDF에서 도출한 계산·판정·조회 도구 후보와 입출력 명세, 검증 예시
  * 문서상 한계 및 담당 부서 확인 필요 사항
* 🧭 **[RAG 챗봇 설계 아이디어 노트](docs/RAG_DESIGN_NOTES.md)**:
  * 문서 → 청크 → 도구 → 멀티턴 역질문의 계층형 타게팅 구조, 위험과 보완
  * 라우팅 실패 대응 흐름(후보 선택 → 검색 대체 경로 → 참조문서 없음), 논의 변경 이력

## Agent Orchestration Viewer (프로토타입)

`seed32-think`(HyperCLOVAX-SEED-Think-32B, vLLM) 기반 에이전트의 오케스트레이션 흐름과 MCP 도구 호출을 실시간으로 시각화하는 웹 UI입니다.

```
React (Vite, :5173) ──SSE──▶ FastAPI (:8000) ──▶ LangGraph [planner → agent ⇄ tools]
                                                     │             │
                                     ChatVLLM(seed32-think)   langchain-mcp-adapters
                                                                   ├─▶ FastMCP docs     (:8101) get_training_policy
                                                                   └─▶ FastMCP training (:8102) calculate_training_credits
```

### 실행

```bash
# 프로젝트 루트에 worker-ai-endpoints.env (LLM_BASE_URL 등, 커밋 금지)를 둔다
uv sync
(cd web && npm install)
./scripts/dev.sh        # → http://localhost:5173
```

### 구성

| 경로 | 내용 |
| :--- | :--- |
| `mcp_servers/` | FastMCP 서버 2종: `docs`(규정 원문 조회), `training`(교육훈련 판정·계산). 범용 계산·문서검색·이메일 mock 도구는 규정 Q&A에서 도구 선택을 흐려 제거했다 |
| `get_training_policy` / `calculate_training_credits` | 인사팀 개인교육훈련비 지원 기준 조회, 이수학점 계산. 판단에 필요한 정보가 없으면 `need_info`와 역질문을 반환하고, 에이전트가 사용자에게 되묻는다 (같은 `thread_id`로 멀티턴 이어짐) |
| `backend/graph.py` | LangGraph 그래프. 각 노드가 `get_stream_writer()`로 시각화 이벤트를 발행 |
| `backend/app.py` | `GET /api/graph` (그래프 구조·MCP 도구 목록), `POST /api/chat` (실행 이벤트 SSE) |
| `web/` | 채팅 / React Flow 그래프 / 실행 타임라인 3분할 UI |

### SSE 이벤트

`run_start` · `node_start` / `node_end` · `plan` · `thinking` / `token` (스트리밍 델타) · `llm_end` (도구 호출 결정, 토큰 수, tok/s) · `tool_call` / `tool_result` (MCP 서버명, 인자, 결과, 소요 시간) · `error` · `run_end`

### 주의사항

* `langchain-mcp-adapters`가 `mcp<2`를 요구하므로 `fastmcp`는 3.x로 고정되어 있습니다 (fastmcp 4.x는 `mcp>=2`).
* LLM은 OpenAI 호환 vLLM 서버를 사용합니다. 서버 주소는 인증 없는 접근권이라 코드에 두지 않고, `backend/config.py`가 루트의 `worker-ai-endpoints.env`(`.gitignore` 대상)에서 읽습니다. 환경변수가 있으면 환경변수가 우선합니다.
* `ChatOpenAI`는 vLLM의 `reasoning_content`(Thinking)를 버리므로 `backend/llm.py`의 `ChatVLLM`이 이를 보존하고 다음 요청에 되돌려 보냅니다. Thinking on/off는 `chat_template_kwargs.thinking`으로 전달합니다.
* 컨텍스트는 요청당 32,768토큰(입력+출력 합)이고, 서버 동시 처리는 4요청입니다.
* 생성 상한을 둡니다(`LLM_MAX_TOKENS=4096`, planner는 `LLM_PLANNER_MAX_TOKENS=512`). planner는 JSON 모드(`response_format`)를 쓰지 않습니다. seed32-think가 JSON 모드에서 가끔 끝나지 않고 상한까지 생성하기 때문입니다.
