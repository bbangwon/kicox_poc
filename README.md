# KICOX 내부규정 RAG

KICOX 내부규정을 PostgreSQL · Qdrant에 적재하고(`ingest`), 적재된 규정으로 검색 · 답변하는 RAG 챗봇(`backend` · `web`)을 개발하고 검증하는 PoC입니다. 규정 전처리(청킹 · 요약)는 [kicox_regulation_prep](../kicox_regulation_prep)에서 하고, 그 JSON 산출물을 받아 적재합니다. LLM · 임베딩은 로컬 인프라(DGX)의 서버를 씁니다.

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

## DB 적재

전처리 산출물(`../kicox_regulation_prep/data/docs/{doc_id}/`)을 PostgreSQL에 넣고(`load`), PostgreSQL에서 Qdrant 포인트를 만들어 색인합니다(`sync`). 설계와 단계별 규칙은 [DB_LOADING_PLAN.md](docs/DB_LOADING_PLAN.md) §7입니다.

### 준비

* 로컬 공용 컨테이너 `comwel-postgres-local`(PostgreSQL) · `comwel-qdrant-local`(Qdrant)이 떠 있어야 합니다.
* 루트에 `.env`(`.env.example` 복사, `KICOX_DATABASE_URL` · `QDRANT_URL`)와 `worker-ai-endpoints.env`(`EMBED_BASE_URL`)를 둡니다. 둘 다 커밋 금지. `EMBED_BASE_URL`이 없으면 dense 없이 BM25만 색인합니다.
* 산출물 위치가 기본값과 다르면 `.env`의 `INGEST_SRC`나 `load --src`로 지정합니다.

### 처음 적재할 때

```bash
uv sync
uv run python -m ingest migrate            # DB kicox 생성 · 마이그레이션 적용
uv run python -m ingest load --dry-run     # 관문 판정만. 통과 · 탈락 건수와 사유를 확인
uv run python -m ingest load               # 통과한 판을 PostgreSQL에 적재, PDF · 쪽 이미지는 storage/docs/로 복사
uv run python -m ingest sync               # 첫 실행은 avgdl 실측값을 출력하고 멈춤 (종료 코드 2)
# 출력된 RAG_BM25_AVGDL_CATALOG · RAG_BM25_AVGDL_CHUNKS를 .env에 적은 뒤
uv run python -m ingest sync               # Qdrant 색인 → 현행 판 전환 → 점검
```

`sync` 마지막 줄이 `점검: 정상`이면 끝입니다. 이후 `./scripts/dev.sh`로 테스트 화면을 띄웁니다.

### 산출물이 바뀌었을 때

전처리 뷰어에서 재청킹 · 요약 재생성 · 검증 · 도구 승인을 했으면 같은 두 명령을 다시 실행합니다.

```bash
uv run python -m ingest load [--doc ID ...]   # 바뀐 판만 다시 적재 (묶음 해시가 같으면 건너뜀)
uv run python -m ingest sync [--doc ID ...]   # 입력이 바뀐 포인트만 다시 임베딩, 검증 표시만 바뀌면 payload만 갱신
```

* 화면의 "검증 전 수치" 표시는 `chunks.verified`를 읽습니다. `verify.json`을 고쳐도 `load`를 다시 돌리기 전에는 바뀌지 않습니다.
* `--doc`을 빼면 전체가 대상입니다. 바뀌지 않은 판은 건너뛰므로 전체로 돌려도 됩니다.
* `avgdl`을 바꾸면 BM25 값이 모두 달라지므로 전체 재색인이 필요합니다(§7.6).

### 명령

| 명령 | 하는 일 | 종료 코드 |
| :--- | :--- | :--- |
| `migrate` | DB 생성, `db/migrations/`의 SQL을 이름 순으로 한 번씩 적용 | 0 |
| `load [--src DIR] [--doc ID ...] [--dry-run]` | 관문 검사 → 판 1건씩 트랜잭션으로 적재. `--dry-run`은 DB에 쓰지 않음 | 0 (탈락이 있어도 0, 출력의 탈락 사유를 확인) |
| `sync [--doc ID ...]` | `LOADED` · `INDEXED` 판을 Qdrant에 동기화, 현행 판 전환, 점검 | 0 정상 · 1 실패 판이나 점검 문제 · 2 avgdl 미설정 |
| `check` | PostgreSQL ↔ Qdrant 점검만 | 0 정상 · 1 문제 · 2 avgdl 미설정 |

`sync`가 중간에 실패한 판은 `LOADED`로 남고, 다시 실행하면 색인되지 않은 행부터 이어서 합니다.

## 내부규정 Q&A 테스트 화면

적재된 규정(PostgreSQL + Qdrant)으로 검색과 답변 생성을 시험하는 챗봇 화면입니다. 디자인은 원 프로젝트(kicox-poc) 국회대응 답변 작성 화면의 토큰 · 대화창 · 근거 카드를 따릅니다. 흐름은 [RAG_FLOW_OVERVIEW.md](docs/RAG_FLOW_OVERVIEW.md) §4입니다.

```
React (Vite, :5173) ──SSE──▶ FastAPI (:8000) ──▶ LangGraph
  analyze ─▶ route ─▶ plan ─▶ read ─▶ (tool) ─▶ answer
     │         │  ▲     │                 └──▶ ask (값이 비면 되묻고 턴 종료, 최대 2회)
     └─────────┴──┼─────┴──▶ no_reference      (plan에서 근거 없음 → route 1회 재시도)
                  │
  다음 턴이 역질문의 답이면: resume ─▶ (계산) ─▶ answer | ask   (새 질문이면 resume ─▶ analyze)
      backend/search.py ── Qdrant reg_catalog · reg_chunks (dense + BM25, 앱 RRF) · PostgreSQL 재확인 · 원문
```

### 실행

```bash
# 루트에 worker-ai-endpoints.env(LLM_BASE_URL · EMBED_BASE_URL)와 .env(KICOX_DATABASE_URL 등) — 둘 다 커밋 금지
# 적재 · 색인이 먼저 되어 있어야 한다 (위 "DB 적재")
uv sync
(cd web && npm install)
./scripts/dev.sh        # → http://localhost:5173
```

### 구성

| 경로 | 내용 |
| :--- | :--- |
| `backend/search.py` | `search_docs`(문서 후보) · `search_chunks`(절 · child 적중, kind별 top-k) · `list_sections` · `get_chunks`(앞뒤 이웃 포함) · `list_tools`(선택 문서의 승인 도구). 임베딩 · BM25는 `ingest`와 같은 함수를 쓴다 |
| `backend/graph.py` | 노드 9개. analyze · route · plan · tool · resume은 thinking off 짧은 JSON 호출, answer는 근거만으로 스트리밍 답변(문장 끝 `[n]` 인용) |
| `backend/tools_exec.py` | 도구 spec 실행기. 전처리 저장소 `preprocess/tools_exec.py` 사본 (원본을 고치면 같이 맞춘다) |
| `backend/app.py` | `GET /api/info` (모델 · 적재 문서 · 임베딩 사용 여부), `POST /api/chat` (실행 이벤트 SSE, `thread_id`로 멀티턴) |
| `web/` | ① 테스트 시나리오(`src/scenarios.ts`) · ② 대화 · ③ 근거 카드 / 처리 단계(검색 후보 · 순위 · LLM 판단) 3열 화면. 토큰 · 글꼴은 원 프로젝트에서 복사(`src/styles`, `src/fonts`) |

문서별 계산 도구 (설계 노트 §5.4-6~7):
- 선택 문서에 승인 도구(`document_tools`)가 있으면 read 뒤 tool 노드가 도구 · 입력값을 고르고 실행기가 계산합니다. LLM은 사용자가 말한 값과 근거 원문에서 바로 정해지는 값만 넣고, 모르는 값은 비워 둡니다. 앞 호출 결과를 `"#1"`로 다음 호출에 넘길 수 있습니다 (최대 3개)
- 계산 결과는 근거 목록 끝에 번호를 붙여 넣고(`kind: "tool"`), 답변은 원문처럼 `[n]`으로 인용합니다
- 값이 비면 ask가 되묻고 턴을 끝냅니다. 다음 턴은 resume이 그 메시지가 답인지 판정해, 답이면 다시 검색하지 않고 계산하고 새 질문이면 처음부터 처리합니다. 2번 되물어도 비거나 모른다고 하면 계산 없이 조건부로 답합니다

### SSE 이벤트

`run_start` · `node_start` / `node_end` · `analysis`(범위 밖 판정, 재작성 질의) · `doc_candidates`(문서 후보, 신호별 순위, RRF 점수) · `route`(선택 문서, 이유) · `chunk_hits`(절 적중) · `plan`(읽을 child, 이유) · `evidence`(근거 원문 · 계산 결과, 번호) · `tools`(선택 문서의 승인 도구) · `tool_calls`(고른 도구 · 입력값) · `tool_results`(계산 결과, 빈 값) · `ask`(역질문) · `resume`(역질문 답 판정) · `thinking` / `token` (스트리밍 델타) · `llm_end` (토큰 수, tok/s) · `llm_retry` · `no_reference` · `error` · `run_end`

### 주의사항

* LLM은 OpenAI 호환 vLLM 서버를 사용합니다. 서버 주소는 인증 없는 접근권이라 코드에 두지 않고, `backend/config.py`가 루트의 `worker-ai-endpoints.env`(`.gitignore` 대상)에서 읽습니다. 환경변수가 있으면 환경변수가 우선합니다.
* `ChatOpenAI`는 vLLM의 `reasoning_content`(Thinking)를 버리므로 `backend/llm.py`의 `ChatVLLM`이 이를 보존합니다. Thinking on/off는 `chat_template_kwargs.thinking`으로 전달하며, 화면의 토글은 answer 노드에만 적용됩니다.
* 컨텍스트는 요청당 32,768토큰(입력+출력 합)이고, 서버 동시 처리는 4요청입니다. planner는 선택 문서의 목차와 child 머리말 전체를 받으므로 문서가 커지면 이 한도를 다시 봐야 합니다.
* 생성 상한을 둡니다(`LLM_MAX_TOKENS=4096`, 분석 · 선택 호출은 `LLM_PLANNER_MAX_TOKENS=512`). JSON 모드(`response_format`)를 쓰지 않습니다. seed32-think가 JSON 모드에서 가끔 끝나지 않고 상한까지 생성하기 때문입니다.
* 기존 교육훈련비 MCP 서버(`mcp_servers/`)와 오케스트레이션 뷰어는 이 화면으로 대체하며 제거했습니다(git 기록에 남음).
