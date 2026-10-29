# VDB 스펙 (Qdrant)

본 문서는 `kicox_regulation_rag`가 쓰는 Qdrant 컬렉션의 **현재 구현 기준 명세**입니다. 설계 근거는 [DB_LOADING_PLAN.md](DB_LOADING_PLAN.md) §6 · §9에 있고, 원본 데이터(PostgreSQL)는 [RDB_SPEC.md](RDB_SPEC.md)를 봅니다.

- **기준일**: 2026-10-29
- **기준 코드**: `ingest/config.py` · `points.py` · `sync.py` · `sparse.py` · `embed.py`, `backend/search.py`
- **역할**: 검색 인덱스. **PostgreSQL만으로 다시 만들 수 있어야 하고**, 사람이 직접 고치지 않습니다. 검색 결과는 항상 PostgreSQL에서 현행 판인지 다시 확인한 뒤 씁니다

---

## 1. 접속

| 항목 | 값 |
| :--- | :--- |
| 서버 | 로컬 공용 컨테이너 `comwel-qdrant-local` (Qdrant 1.15.4), 1대 |
| 주소 | 환경변수 `QDRANT_URL` (기본 `http://localhost:6333`) |
| 클라이언트 | `qdrant_client.QdrantClient` (동기, REST) |
| 같은 서버의 다른 컬렉션 | 이 프로젝트는 `reg_*` 별칭과 그 물리 컬렉션만 건드립니다 |

---

## 2. 컬렉션

### 2.1 별칭과 물리 이름

앱(로더 · 백엔드)은 **별칭으로만** 읽고 씁니다.

| 별칭 | 용도 | 물리 이름 (임베딩 있음) | 물리 이름 (임베딩 없음) |
| :--- | :--- | :--- | :--- |
| `reg_catalog` | router: 문서 후보 고르기 | `reg_catalog__bge-m3-ko__1024` | `reg_catalog__bm25__0` |
| `reg_chunks` | planner: 선택 문서 안 절 · 청크 찾기 | `reg_chunks__bge-m3-ko__1024` | `reg_chunks__bm25__0` |

- 물리 이름 규칙: `{별칭}__{EMBED_MODEL}__{EMBED_DIM}`, `EMBED_BASE_URL`이 없으면 `{별칭}__bm25__0` (`config.physical_name`). 모델 · 차원을 따로 기록하지 않고 이름으로 구분합니다.
- 별칭이 이미 다른 물리 컬렉션을 가리키거나, 물리 컬렉션의 벡터 설정(§2.2)이 현재 설정과 다르면 `CollectionMismatch`로 멈춥니다(`sync.ensure_collections`).
- 컬렉션 · 별칭 · payload 인덱스는 `ingest sync`가 처음 실행될 때 만듭니다.
- 모델 교체 · 재색인용 `reindex` · `switch-alias` 명령은 아직 없습니다(DB_LOADING_PLAN §10 7단계).

### 2.2 벡터 설정

두 컬렉션이 같습니다.

| 이름 | 종류 | 설정 | 비고 |
| :--- | :--- | :--- | :--- |
| `dense` | named dense vector | `size = 1024`, `distance = COSINE` | 임베딩이 없으면(`EMBED_BASE_URL` 미설정) 만들지 않음 |
| `bm25` | named sparse vector | `modifier = IDF` | IDF는 Qdrant가 곱하고, 문서 값은 앱이 BM25 TF 포화로 계산 (§5) |

HNSW · 양자화 · 샤드 · 복제는 Qdrant 기본값입니다.

### 2.3 payload 인덱스

| 키 | 스키마 | `reg_catalog` | `reg_chunks` |
| :--- | :--- | :---: | :---: |
| `kind` | `KEYWORD` | O | O |
| `doc_id` | `KEYWORD` | O | O |
| `is_current` | `BOOL` | O | O |
| `section_id` | `KEYWORD` | | O |
| `verified` | `BOOL` | | O |

`text` · `dense_text`에는 인덱스를 두지 않습니다.

---

## 3. 포인트 종류

| 컬렉션 | `kind` | 개수 | 포인트 ID | 원본 행 (`indexed_hash` 위치) |
| :--- | :--- | :--- | :--- | :--- |
| `reg_catalog` | `doc_summary` | 판당 1 | `uuid5(NS, "{doc_id}:doc_summary")` | `documents` |
| `reg_catalog` | `doc_question` | 판당 `len(documents.questions)` | `uuid5(NS, "{doc_id}:doc_question:{i}")`, `i`는 0부터 | `documents` |
| `reg_chunks` | `parent_summary` | `searchable AND covers IS NOT NULL`인 section당 1 | `uuid5(NS, "{section_id}:summary")` | `sections` |
| `reg_chunks` | `child` | `searchable`인 chunk당 1 | `chunks.id` 그대로 | `chunks` |

- `NS`는 PostgreSQL ID와 같은 값입니다([RDB_SPEC.md](RDB_SPEC.md) §5).
- **포인트가 있는 판**: `status IN ('LOADED','INDEXED')`. `SUPERSEDED` · `REPEALED` 판은 포인트가 없어야 합니다. 새 판은 현행 판 전환 전까지 `is_current = false`로 들어가 있습니다.
- **넣지 않는 것**: `not_for` · `not_covers`, `searchable = false`인 parent · child.

---

## 4. 포인트 내용

### 4.1 임베딩 입력 텍스트

| `kind` | dense 입력 | sparse(BM25) 입력 |
| :--- | :--- | :--- |
| `doc_summary` | `{name}\n{summary}\n키워드: {keywords를 ", "로}` | dense와 같음 |
| `doc_question` | `{question}` | dense와 같음 |
| `parent_summary` | `[{문서 name} > {section title}]\n{covers}\n키워드: {keywords를 ", "로}` + (질문이 있으면) `\n예상 질문: {questions를 " / "로}` | dense와 같음 |
| `child` | `[{compact_header(header)}]\n{body}` | `{body}` (원문만) |

- `compact_header`는 `" > "`로 나눈 항목 중 **연속으로 반복되는 항목을 하나로** 줄입니다(예: `보수규정 시행세칙 > 보수규정 시행세칙 > 제1조` → `보수규정 시행세칙 > 제1조`). 항목 안의 `>`(예: `부칙 <2011.09.14.>`)는 자르지 않습니다.
- 입력 형식은 `EMBED_TEXT_VERSION`(현재 1)으로 관리합니다. 바꾸면 입력 해시가 모두 달라져 전체 재색인됩니다.

### 4.2 payload

| 키 | 형식 | `doc_summary` | `doc_question` | `parent_summary` | `child` | 값 |
| :--- | :--- | :---: | :---: | :---: | :---: | :--- |
| `kind` | string | O | O | O | O | §3의 `kind` |
| `doc_id` | string | O | O | O | O | `documents.id` |
| `is_current` | bool | O | O | O | O | `documents.is_current` |
| `section_id` | string (uuid) | | | O | O | `sections.id` |
| `verified` | bool | | | | O | `chunks.verified` |
| `text` | string | | | | O | `chunks.body` (sparse 입력과 같음) |
| `dense_text` | string | | | | O | 실제 dense 입력 (§4.1) |

예시 (`reg_chunks`의 `child`):

```json
{
  "doc_id": "e1f17de5627c",
  "is_current": true,
  "kind": "child",
  "section_id": "1b0c…",
  "verified": false,
  "text": "제3조(적용범위) …",
  "dense_text": "[보수규정 시행세칙 > 제1장 총칙 > 제3조(적용범위)]\n제3조(적용범위) …"
}
```

- 쪽번호 · 제목 · 수치 여부는 payload에 없습니다. 근거 표시는 PostgreSQL 재확인 쿼리에서 읽습니다.
- `text` · `dense_text`는 child ID가 `header + text`의 해시라 같은 ID에서 PostgreSQL과 어긋날 일이 없습니다. 리랭커 입력(`dense_text`) · 미리보기용으로 두었고, 리랭커가 아직 없어 지금 검색 코드는 읽지 않습니다.
- `parent_summary`에는 `verified`가 없습니다. `verified` 필터를 `must`로 걸면 parent 요약 포인트가 빠지므로, 엄격 모드를 평가할 때는 `kind = 'child'`에만 걸어야 합니다.

---

## 5. 벡터 생성 규칙

### 5.1 dense

| 항목 | 값 | 설정 |
| :--- | :--- | :--- |
| 공급자 | AI 서버 vLLM OpenAI 호환 `POST {EMBED_BASE_URL}/embeddings` | `EMBED_BASE_URL` (`worker-ai-endpoints.env`) |
| 모델 · 차원 | `bge-m3-ko`, 1024 | `EMBED_MODEL` · `EMBED_DIM` |
| 접두어 | 질의 · 문서 모두 없음 | `EMBED_QUERY_PREFIX` · `EMBED_DOCUMENT_PREFIX` |
| 최대 입력 | 8192 토큰 (`truncate_prompt_tokens`로 서버에서 자름) | `EMBED_MAX_TOKENS` |
| 배치 · 동시성 · 타임아웃 | 32 · 2 · 60초 | `EMBED_BATCH_SIZE` · `EMBED_MAX_CONCURRENCY` · `EMBED_TIMEOUT_SECONDS` |
| 재시도 | 연결 실패 · 429 · 5xx만, 최대 3회, 1 · 2 · 4초 대기 | `EMBED_MAX_RETRIES` |
| 검증 | 응답 개수나 차원이 하나라도 다르면 배치 전체 실패 (섞인 벡터 금지) | |

색인과 질의가 같은 `ingest.embed.get_embedder()`를 씁니다.

### 5.2 sparse (BM25)

색인과 질의가 같은 `ingest.sparse.tokens()`를 써야 합니다. 규칙을 바꾸면 전체 재색인이 필요합니다.

| 항목 | 규칙 |
| :--- | :--- |
| 낱말 | 소문자로 바꾼 뒤 `[0-9A-Za-z가-힣]+` |
| 제외 | 한 글자 낱말, 불용어(`및 등 또는 그 이 해당 경우 관한 관하여 따른 따라 위한 의한 의하여 한다 하는 하여 된다 있다 없다 있는 없는 수 것 때 각`) |
| 2-gram | 한글이 든 세 글자 이상 낱말은 낱말 자체 + 글자 2-gram을 더함 (2-gram도 불용어 제외) |
| 인덱스 | `CRC32(토큰 UTF-8)`. 충돌하면 값을 더함 |
| 문서 값 | `tf · (k1 + 1) / (tf + k1 · (1 − b + b · dl / avgdl))`, `k1 = 1.2`, `b = 0.75`, `dl` = 그 포인트의 토큰 수 |
| 질의 값 | 질의 토큰(중복 제거)마다 1.0 |
| IDF | 컬렉션의 `modifier = IDF`가 곱함 |
| `avgdl` | 컬렉션별 고정값. `RAG_BM25_AVGDL_CATALOG` · `RAG_BM25_AVGDL_CHUNKS` (`.env`). 비어 있으면 `sync`가 실측값(`LOADED` · `INDEXED` 판의 포인트별 sparse 토큰 수 평균, child는 원문 기준)을 알려 주고 멈춤 |

현재 `avgdl`은 로컬 2건 실측값(catalog 43.4 · chunks 110.2)인 임시값입니다. ALIO 138건을 적재하면 다시 재고 전체 재색인합니다.

---

## 6. 입력 해시와 동기화

### 6.1 입력 해시

```text
sha256( "{EMBED_TEXT_VERSION}\0{물리 컬렉션 이름}\0{avgdl}" + Σ "\0" + 입력 )[:16]
```

| 포인트 | 입력 | 저장 위치 |
| :--- | :--- | :--- |
| catalog (`doc_summary` + `doc_question`) | 판 하나의 모든 catalog 포인트 dense 입력을 순서대로. **묶음 해시 하나** | `documents.indexed_hash` |
| `parent_summary` | dense 입력, sparse 입력 | `sections.indexed_hash` |
| `child` | dense 입력, sparse 입력 | `chunks.indexed_hash` |

입력 형식 버전 · 물리 컬렉션(모델 · 차원) · `avgdl`이 바뀌어도 해시가 달라져 다시 색인됩니다. catalog는 묶음이라 요약이 바뀌면 질문 포인트까지 함께 다시 임베딩합니다.

### 6.2 `ingest sync` 순서 (`sync.sync_doc`, 문서 판마다)

1. PostgreSQL 행에서 "있어야 할 포인트" 목록과 입력 해시를 만듭니다(`points.build`, 산출물 파일은 읽지 않음).
2. **입력 해시 ≠ `indexed_hash`** 인 포인트만 임베딩 → `upsert(wait=True)` → `indexed_hash` 갱신. 64개 배치마다 커밋합니다.
3. **입력 해시 = `indexed_hash`** 인 포인트는 벡터를 두고 payload 전체를 덮어씁니다(`batch_update_points` + `OverwritePayloadOperation`, 컬렉션마다 요청 1개). `is_current` · `verified`, 재청킹으로 parent ID가 바뀐 형제 child의 `section_id`가 여기서 바뀝니다.
4. `scroll(filter doc_id)`로 그 판의 포인트 ID를 훑어 1의 목록에 없는 포인트를 지웁니다(재청킹으로 사라진 청크).
5. 성공하면 PostgreSQL에서 `LOADED` → `INDEXED`.

### 6.3 현행 판 전환 (`sync.switch_current`, 규정마다)

PostgreSQL 트랜잭션([RDB_SPEC.md](RDB_SPEC.md) §4)을 커밋한 뒤:

| 대상 | Qdrant 작업 |
| :--- | :--- |
| 새 현행 판 | `set_payload({"is_current": true}, filter doc_id)` (두 컬렉션) |
| `SUPERSEDED`가 된 판 | `delete(filter doc_id)` (두 컬렉션) |

커밋과 Qdrant 갱신 사이에는 두 판 포인트가 함께 검색될 수 있지만, 검색 결과를 PostgreSQL `is_current`로 다시 거르므로 답변에는 섞이지 않습니다.

---

## 7. 검색 (질의 처리)

`backend/search.py`. Qdrant 내장 fusion은 쓰지 않고 **앱에서 RRF(k = 60)** 로 합칩니다. 모든 검색에 `is_current = true` 필터를 겁니다.

| 단계 | 컬렉션 | 필터 | 신호 · top-k | 합치기 | 이후 |
| :--- | :--- | :--- | :--- | :--- | :--- |
| router `search_docs(queries)` | `reg_catalog` | `is_current` | 질의마다 `dense` 40 + `bm25` 40 (`CATALOG_TOPK`) | 질의 × 신호 순위 목록을 포인트 단위 RRF → `doc_id`별 최고점 | 상위 20개(`DOC_CANDIDATES`)를 PostgreSQL에서 `status='INDEXED' AND is_current`로 재확인 |
| planner `search_chunks(query, doc_ids)` | `reg_chunks` | `is_current`, `doc_id ∈ 선택 문서`, `kind` | `kind`별로 따로: `child` 20, `parent_summary` 10 (`CHUNK_TOPK`), 각각 `dense` + `bm25` | `kind` × 신호 순위 목록을 RRF → `section_id`별 최고점 | 절 단위 적중(요약 적중 여부 + 적중 child 목록), 원문은 PostgreSQL에서 읽음 |

- 임베딩이 없으면 `bm25` 신호만 씁니다.
- `kind`별로 따로 뽑는 이유: 한 번에 검색하면 BM25 상위를 키워드 · 예상 질문이 붙은 parent 요약이 차지합니다(DB_LOADING_PLAN §11-10).
- 리랭커(`bge-reranker-v2-m3-ko`, 입력 `dense_text`)는 아직 연결하지 않았습니다.

---

## 8. 정합성 점검

`uv run python -m ingest check` (sync 끝에도 실행).

| 문서 판 상태 | 기대 포인트 수 (컬렉션별, `count(filter doc_id, exact)`) |
| :--- | :--- |
| `LOADED` | 점검하지 않음 (다음 sync가 이어서 함) |
| `INDEXED` | `points.build`로 PostgreSQL에서 계산한 수와 같음 |
| `SUPERSEDED` · `REPEALED` | 0 |

PostgreSQL 쪽 불변식은 [RDB_SPEC.md](RDB_SPEC.md) §8.

---

## 9. 현재 규모 (로컬, 2026-10-28)

| 문서 | `reg_catalog` | `reg_chunks` |
| :--- | :---: | :---: |
| 맞춤형복리후생제도 운영지침 (`46e9cf81e6af`) | 9 | 33 |
| 보수규정 시행세칙 (`e1f17de5627c`) | 9 | 106 |
| 합계 | 18 | 139 |

138건(평균 10쪽 가정) 적재 시 child 약 6천 · 포인트 약 7천 개로 예상합니다(DB_LOADING_PLAN §10).

---

## 10. 다시 색인되는 경우

| 바뀐 것 | 다시 임베딩되는 범위 | 방법 |
| :--- | :--- | :--- |
| child 본문 · 머리말 (재청킹) | 바뀐 child + 그 parent 요약 | ID가 바뀌어 새 포인트 upsert, 옛 포인트 삭제 |
| parent 요약 | 그 `parent_summary` 포인트 | `sync` |
| 문서 요약 · 예상 질문 | 그 판의 catalog 포인트 전체 | `sync` |
| `verified` · `is_current` | 없음 | payload 덮어쓰기 |
| `EMBED_TEXT_VERSION` · `avgdl` | 전체 | `sync` (입력 해시가 모두 바뀜) |
| 임베딩 모델 · 차원 | 전체 | 새 물리 컬렉션을 PostgreSQL에서 채운 뒤 별칭 전환 (명령 미구현, §2.1) |
| BM25 토큰 규칙 (`sparse.py`) | 전체 | 입력 해시에 들어가지 않으므로 `EMBED_TEXT_VERSION`을 함께 올리거나 새 컬렉션으로 |

---

## 11. 변경 이력

| 날짜 | 내용 |
| :--- | :--- |
| 2026-10-29 | 최초 작성: `ingest/` · `backend/search.py` 구현 기준으로 컬렉션 · 벡터 · payload · 포인트 · 벡터 생성 · 동기화 · 검색 · 점검 정리 |
