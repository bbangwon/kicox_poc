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

## 내부규정 Q&A 테스트 화면

적재된 규정(PostgreSQL + Qdrant)으로 검색과 답변 생성을 시험하는 챗봇 화면입니다. 디자인은 원 프로젝트(kicox-poc) 국회대응 답변 작성 화면의 토큰 · 대화창 · 근거 카드를 따릅니다. 흐름은 [RAG_FLOW_OVERVIEW.md](docs/RAG_FLOW_OVERVIEW.md) §4입니다.

```
React (Vite, :5173) ──SSE──▶ FastAPI (:8000) ──▶ LangGraph
  analyze ─▶ route ─▶ plan ─▶ read ─▶ answer
     │         │  ▲     │
     └─────────┴──┼─────┴──▶ no_reference      (plan에서 근거 없음 → route 1회 재시도)
                  │
      backend/search.py ── Qdrant reg_catalog · reg_chunks (dense + BM25, 앱 RRF) · PostgreSQL 재확인 · 원문
```

### 실행

```bash
# 루트에 worker-ai-endpoints.env(LLM_BASE_URL · EMBED_BASE_URL)와 .env(KICOX_DATABASE_URL 등) — 둘 다 커밋 금지
# 적재 · 색인이 먼저 되어 있어야 한다: uv run python -m ingest load && uv run python -m ingest sync
uv sync
(cd web && npm install)
./scripts/dev.sh        # → http://localhost:5173
```

### 구성

| 경로 | 내용 |
| :--- | :--- |
| `backend/search.py` | `search_docs`(문서 후보) · `search_chunks`(절 · child 적중, kind별 top-k) · `list_sections` · `get_chunks`(앞뒤 이웃 포함). 임베딩 · BM25는 `ingest`와 같은 함수를 쓴다 |
| `backend/graph.py` | 노드 6개. analyze · route · plan은 thinking off 짧은 JSON 호출, answer는 근거만으로 스트리밍 답변(문장 끝 `[n]` 인용) |
| `backend/app.py` | `GET /api/info` (모델 · 적재 문서 · 임베딩 사용 여부), `POST /api/chat` (실행 이벤트 SSE, `thread_id`로 멀티턴) |
| `web/` | ① 테스트 시나리오(`src/scenarios.ts`) · ② 대화 · ③ 근거 카드 / 처리 단계(검색 후보 · 순위 · LLM 판단) 3열 화면. 토큰 · 글꼴은 원 프로젝트에서 복사(`src/styles`, `src/fonts`) |

문서별 계산 도구와 역질문은 승인된 도구가 생기면 plan 뒤에 붙입니다(설계 노트 §5.4-6~7).

### SSE 이벤트

`run_start` · `node_start` / `node_end` · `analysis`(범위 밖 판정, 재작성 질의) · `doc_candidates`(문서 후보, 신호별 순위, RRF 점수) · `route`(선택 문서, 이유) · `chunk_hits`(절 적중) · `plan`(읽을 child, 이유) · `evidence`(근거 원문, 번호) · `thinking` / `token` (스트리밍 델타) · `llm_end` (토큰 수, tok/s) · `llm_retry` · `no_reference` · `error` · `run_end`

### 주의사항

* LLM은 OpenAI 호환 vLLM 서버를 사용합니다. 서버 주소는 인증 없는 접근권이라 코드에 두지 않고, `backend/config.py`가 루트의 `worker-ai-endpoints.env`(`.gitignore` 대상)에서 읽습니다. 환경변수가 있으면 환경변수가 우선합니다.
* `ChatOpenAI`는 vLLM의 `reasoning_content`(Thinking)를 버리므로 `backend/llm.py`의 `ChatVLLM`이 이를 보존합니다. Thinking on/off는 `chat_template_kwargs.thinking`으로 전달하며, 화면의 토글은 answer 노드에만 적용됩니다.
* 컨텍스트는 요청당 32,768토큰(입력+출력 합)이고, 서버 동시 처리는 4요청입니다. planner는 선택 문서의 목차와 child 머리말 전체를 받으므로 문서가 커지면 이 한도를 다시 봐야 합니다.
* 생성 상한을 둡니다(`LLM_MAX_TOKENS=4096`, 분석 · 선택 호출은 `LLM_PLANNER_MAX_TOKENS=512`). JSON 모드(`response_format`)를 쓰지 않습니다. seed32-think가 JSON 모드에서 가끔 끝나지 않고 상한까지 생성하기 때문입니다.
* 기존 교육훈련비 MCP 서버(`mcp_servers/`)와 오케스트레이션 뷰어는 이 화면으로 대체하며 제거했습니다(git 기록에 남음).
