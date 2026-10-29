# 원 프로젝트(국회대응) RAG · 임베딩 정책 참고 노트

본 문서는 원 프로젝트 `kicox-poc`의 **① 국회대응(assembly)** 서비스가 RAG · 유사도 검색을 어떻게 쓰는지 코드 기준으로 정리한 참고 자료입니다. 이 PoC의 임베딩 정책을 원 프로젝트 정책에 맞춰 만들기 위한 근거로 씁니다.

- **조사 기준**: `kicox-poc` 브랜치 `feature/regulation`(커밋 `9e35025`), 2026-10-28
- **모델 값 출처**: `deploy/compose/dev/.env.example`. 개발기 · 운영 서버의 실제 `.env` 값은 확인하지 않았습니다
- **관련 문서**: [RAG_DESIGN_NOTES.md](RAG_DESIGN_NOTES.md), [RAG_FLOW_OVERVIEW.md](RAG_FLOW_OVERVIEW.md)

---

## 1. 요약

| 항목 | 국회대응의 선택 |
| :--- | :--- |
| 검색 방식 | **Hybrid**: dense + sparse(BM25) → **RRF** 융합 → **리랭크** |
| 벡터 DB | Qdrant. 컬렉션 하나에 named vector 2개(`dense`, `bm25`) |
| 임베딩 모델 | `bge-m3-ko`, **1024차원**, COSINE |
| 임베딩 호출 | OpenAI 호환 `POST {EMBED_BASE_URL}/embeddings` (vLLM pooling) |
| Sparse | 외부 모델 없음. **자체 BM25 벡터**(낱말 + 2-gram, CRC32 해시), IDF는 Qdrant `modifier=IDF` |
| 리랭커 | `bge-reranker-v2-m3-ko`, Cohere 호환 `POST {RERANK_BASE_URL}/v1/rerank` |
| 생성 LLM | `seed32-think` (OpenAI 호환) |
| 청킹 | 500토큰, 겹침 80토큰, 쪽 · 구역 경계 우선, 제목 경로 prefix |
| 최종 근거 수 | 후보 40 → 최종 8, 같은 장 최대 3 |
| 임베딩 없을 때 | sparse(BM25)만으로 동작 (`__bm25__0` 컬렉션) |

기능별 사용 여부:

| 기능 | 벡터 검색 | 방식 |
| :--- | :---: | :--- |
| 답변 초안 근거 검색 | O | Hybrid + RRF + 리랭크 (§3) |
| 세부 질문별 근거 찾기 | O | 같은 파이프라인, 질문마다 실행 후 합침 (§3.4) |
| 소관 부서(팀) 추천 | X | 프로세스 내 BM25 사전점수 + 팀별 LLM 판정 (§6) |

---

## 2. 색인(Indexing) 정책

### 2.1 청킹 (`modules/assembly/rag/chunking.py`)

| 규칙 | 값 · 내용 |
| :--- | :--- |
| 크기 | `RAG_CHUNK_TOKENS=500`, 겹침 `RAG_CHUNK_OVERLAP=80` (토큰은 보수적 추정치) |
| 경계 | 청크는 **한 쪽(PDF) · 한 구역(DOCX 제목 · 슬라이드)을 넘지 않는다**. 근거 위치(`p.12`, `2장 …`)를 정확히 하기 위함 |
| 제목 경로 prefix | 청크 앞에 `[문서 제목 > 상위 제목 > 하위 제목]`을 붙인다. 맥락 유지와 어휘 검색 보강 목적 |
| 번호 소제목 | 제목 스타일이 없는 공문 · 보고서는 `1. …`, `가. …`, `□ …` 같은 짧은 줄을 소제목으로 인식해 경로에 넣는다. 표 조각도 상위 소제목을 달고 간다 |
| 고정성 | **청킹 규칙은 모델과 무관하게 고정**. 바꾸면 청크 ID가 바뀌어 기존 근거 링크가 끊긴다 |

### 2.2 임베딩 호출 (`core/embedding/`, `providers/openai.py`)

| 항목 | 값 · 내용 |
| :--- | :--- |
| 공급자 구조 | 교체형. 호출부는 `get_embedder().embed(texts, kind=QUERY \| DOCUMENT)`만 안다. 공급자는 `EMBED_PROVIDER`로 고른다 |
| 질의/문서 접두어 | `EMBED_QUERY_PREFIX`, `EMBED_DOCUMENT_PREFIX`. **bge-m3는 둘 다 빈 값** (e5 계열이면 `query: ` / `passage: `) |
| 배치 | `EMBED_BATCH_SIZE=32`. 응답 순서는 `index`로 맞춘다 |
| 최대 입력 | `EMBED_MAX_TOKENS=8192`. 넘으면 앞부분만 남기고 경고. 서버에도 `truncate_prompt_tokens` 전달 |
| 동시성 | `EMBED_MAX_CONCURRENCY=2` (GPU를 LLM과 공유) |
| 타임아웃 | `EMBED_TIMEOUT_SECONDS=60` |
| 차원 검증 | 응답 벡터 길이 ≠ `EMBED_DIM`이면 오류. **섞인 벡터 금지** |
| 오류 분류 | 연결 실패 · 타임아웃 · 429 · 5xx → 재시도 가능, 그 밖의 4xx → 재시도 불가 |
| 미설정 시 | `EMBED_MODEL` 또는 `EMBED_BASE_URL`이 비면 임베더 없음 → sparse만으로 동작 |

### 2.3 Sparse 벡터 (`modules/assembly/rag/sparse.py`)

임베딩 서버가 sparse(lexical) 벡터를 주지 않으므로 직접 만듭니다.

| 항목 | 내용 |
| :--- | :--- |
| 토큰화 | 공백 단위 낱말 + **낱말 안 문자 2-gram**. 일반 낱말(자료 · 현황 · 제출 · 관련 …) 제외. 형태소 분석기는 쓰지 않음 |
| 토큰 → 인덱스 | `CRC32(UTF-8)`. 재기동과 무관하게 같은 값(결정적). 충돌은 값을 더한다 |
| 문서 값 | BM25 TF 포화 `tf·(k1+1) / (tf + k1·(1 − b + b·dl/avgdl))`, `k1=1.2`, `b=0.75`, `avgdl=700` 상수 |
| 질의 값 | 나온 토큰마다 1.0 |
| IDF | Qdrant 컬렉션 설정 `modifier=IDF`가 곱한다 |
| 주의 | 색인과 질의가 **같은 함수**를 써야 한다. 토큰 규칙을 바꾸면 전체 재색인 필요 |

### 2.4 Qdrant 컬렉션 (`core/qdrant.py`, `modules/assembly/rag/collections.py`)

| 항목 | 내용 |
| :--- | :--- |
| 논리 이름(별칭) | `asm_chunks`. 앱은 별칭으로만 읽고 쓴다 |
| 물리 이름 | `{논리명}__{모델 슬러그}__{차원}` 예: `asm_chunks__bge-m3-ko__1024`. dense가 없으면 `asm_chunks__bm25__0` |
| 벡터 | `dense`(size=`EMBED_DIM`, COSINE) + `bm25`(sparse, `modifier=IDF`) |
| 포인트 ID | `uuid5(NAMESPACE_URL, f"{doc_id}_{chunk_idx}")`. 결정적이라 재색인해도 ID가 같다 |
| 정합성 검사 | 별칭 대상 컬렉션의 모델 · 차원 · sparse 설정이 현재 설정과 다르면 기동 시 오류(`CollectionMismatch`) |
| 페이로드 | 문서 ID · 청크 순번 · 자료 종류 · 부서 · 공개 범위 · 쪽 · 위치 · 파일명 · 제목 · 본문(≤2,000자) · **임베딩 모델명** 등. 키는 데이터 표준 물리명 사용 |
| 필터 인덱스 | keyword: 문서 ID · 자료 종류 · 부서 · 요구사항 · 배정 · 질의서 · 공개 범위 · 참여 부서 · 임베딩 모델 / integer: 청크 순번 |
| 이중화 | 독립 Qdrant 2대. 쓰기는 양쪽, 읽기는 한쪽(헬스 기반 라운드로빈), 한쪽 실패 시 재동기화 큐에 기록 |
| 삭제 단위 | 문서 단위 (`doc_id` 필터) |

### 2.5 모델 교체(재색인) 절차 (`scripts/reindex_vectors.py`)

1. DB에 저장된 청크(`asm_rag_chunks`)로 **새 물리 컬렉션**을 채운다. 원본을 다시 파싱하지 않으므로 청크 ID가 그대로 유지된다.
2. 이 단계에서는 **별칭을 건드리지 않는다**. 앱은 기존 컬렉션으로 계속 검색한다.
3. 평가 세트로 새 모델과 기존 모델을 비교한다.
4. `switch_collection_alias.py`로 별칭을 전환하고, 같은 배포에서 `.env`의 모델 설정을 바꾼다.
5. 재색인 중 새로 들어온 문서는 전환 직전에 한 번 더 실행해 맞춘다(멱등 upsert).
6. 리랭크 점수 컷(`RERANK_MIN_SCORE`)은 모델이 바뀌면 평가 세트로 다시 정한다.

---

## 3. 검색(Retrieval) 파이프라인 (`modules/assembly/rag/retrieval.py`)

### 3.1 흐름

```
요구사항 원문
 ├─ dense 질의 ①: 원문           → Qdrant dense  top 20
 ├─ dense 질의 ②: 규칙 키워드    → Qdrant dense  top 20
 └─ sparse 질의 : 원문(BM25 토큰) → Qdrant sparse top 20
        ↓
   RRF 융합 (k=60) → 후보 40
        ↓
   관계형 재확인 (DB에서 INDEXED 상태인 문서만)
        ↓
   리랭크 (bge-reranker-v2-m3-ko) → 점수 내림차순, 컷 RERANK_MIN_SCORE
        ↓
   출처 다양성 (같은 장 최대 3) → 최종 8
        ↓
   합의 보존 (모든 질의에서 3위 안 → 최대 2개 되살림)
        ↓
   LLM 관련성 판정 → 초안 작성
```

### 3.2 단계별 정책

| 단계 | 값 · 내용 |
| :--- | :--- |
| 질의 구성 | dense 2개(원문, 키워드) + sparse 1개(원문). 키워드는 **LLM 없이 규칙으로** 추출: 숫자 · 1글자 · 일반 낱말 제거, 조사 떼기, 최대 12개 |
| 질의별 top-k | `RAG_TOPK_CANDIDATES // 2` = 20 |
| 융합 | **앱 코드에서 RRF** (`1/(k+rank)` 합산). Qdrant 내장 fusion은 쓰지 않음. `RAG_RRF_K=60` |
| 후보 수 | `RAG_TOPK_CANDIDATES=40` |
| 관계형 재확인 | 벡터 DB 결과를 그대로 믿지 않고 DB에서 `INDEXED` 상태인 문서만 남김(삭제 · 만료 · 재색인 중 배제) |
| 리랭크 | 있으면 적용, 없으면 RRF 순서 사용. `RERANK_MIN_SCORE=0.0` = **현재는 점수 컷 없이 순위만** |
| 출처 다양성 | 같은 **장**(문서 + 제목 경로의 첫 단계 소제목) 최대 `MAX_PER_CHAPTER=3`. 문서 단위가 아니라 장 단위인 이유: 큰 문서 하나에서 근거가 3개로 말라 버리는 회귀를 막기 위함 |
| 최종 수 | `RAG_TOPK_FINAL=8` |
| 합의 보존 | 리랭커가 긴 질의에서 dense · sparse 모두 1위인 청크를 아래로 미는 일이 있어, **세 신호의 합의를 모델 하나의 점수로 뒤집지 않는다**. 모든 질의에서 3위 안에 든 후보는 최대 2개까지 최종에 남김 |

### 3.3 검색 모드

평가용으로 단계를 끄고 켤 수 있습니다: `dense` / `sparse` / `hybrid` / `hybrid+rerank`(기본). 평가 스크립트가 넷을 비교합니다.

### 3.4 세부 질문별 검색

요구사항이 여러 세부 질문으로 나뉘면 질문마다 `retrieve`를 실행(질문별 최종 `DRAFT_PER_QUESTION_TOP`)한 뒤, **순위 차례로**(1위들 → 2위들 → …) 합치고 중복을 제거해 전체 `DRAFT_EVIDENCE_MAX`개까지 남깁니다. 장 상한은 질문별 선택 안에서만 적용합니다. 토큰 예산을 넘으면 뒤(낮은 순위)부터 자릅니다.

### 3.5 검색 범위 필터 (`modules/assembly/rag/filters.py`)

| 자료 종류 | 조건 |
| :--- | :--- |
| `PRE_MATERIAL` (사전자료) | 내 부서 · 상위 부서 · 공단 공통 |
| `ITEM_REFERENCE` (요구사항 참고자료) | 본인 부서 배정의 자료만 |
| `TEAM_MATERIAL` (팀 업무자료) | 사용자가 고른 경우만 |
| `HISTORY_ANSWER`, `ANSWER_ATTACHMENT` (질의답변 이력) | 전부 |

필터는 **필수**입니다(`QdrantPair.search`가 필터 없는 검색을 허용하지 않음).

### 3.6 처리 단계 기록(trace)

각 검색은 질의 · 질의별 후보 · 융합 결과 · 리랭크 전후 순위 · 채택/탈락 사유(`BELOW_MIN_SCORE`, `CHAPTER_LIMIT`, `FINAL_LIMIT`, `CONSENSUS_REPLACED`)와 단계별 소요 시간(ms)을 기록합니다. 본문은 300자 미리보기만 저장합니다.

---

## 4. 리랭커 (`core/rerank/`)

| 항목 | 값 · 내용 |
| :--- | :--- |
| 모델 | `bge-reranker-v2-m3-ko` |
| 프로토콜 | Cohere 호환 `POST {RERANK_BASE_URL}/v1/rerank` (`RERANK_PROVIDER=cohere-v1`, base URL에 `/v1` 없음) |
| 출력 | 입력 순서대로 **0~1 점수**. 로짓이면 어댑터가 sigmoid 변환, 요청당 문서 수 분할도 어댑터가 담당 |
| 동시성 · 타임아웃 | `RERANK_MAX_CONCURRENCY=2`, `RERANK_TIMEOUT_SECONDS=60` |
| 미설정 시 | RRF 순서 그대로 사용 |

---

## 5. 설정 값 모음

| 환경변수 | 기본값 / dev 예시 | 비고 |
| :--- | :--- | :--- |
| `EMBED_PROVIDER` | `openai` | OpenAI 호환 `/v1/embeddings` |
| `EMBED_BASE_URL` | (서버별) | `/v1`까지 포함 |
| `EMBED_MODEL` | `bge-m3-ko` | |
| `EMBED_DIM` | `1024` | |
| `EMBED_BATCH_SIZE` | `32` | |
| `EMBED_MAX_TOKENS` | `8192` | |
| `EMBED_MAX_CONCURRENCY` | `2` | |
| `EMBED_TIMEOUT_SECONDS` | `60` | |
| `EMBED_QUERY_PREFIX` / `EMBED_DOCUMENT_PREFIX` | 빈 값 | bge-m3는 접두어 없음 |
| `RERANK_PROVIDER` | `cohere-v1` | |
| `RERANK_BASE_URL` | (서버별) | `/v1` 없이 |
| `RERANK_MODEL` | `bge-reranker-v2-m3-ko` | |
| `RERANK_MIN_SCORE` | `0.0` | 0 = 컷 없음 |
| `RAG_CHUNK_TOKENS` / `RAG_CHUNK_OVERLAP` | `500` / `80` | |
| `RAG_TOPK_CANDIDATES` / `RAG_TOPK_FINAL` | `40` / `8` | |
| `RAG_RRF_K` | `60` | 평가 세트로 조정 |
| `LLM_MODEL` | `seed32-think` | 생성 · 판정용 |

---

## 6. 참고: 소관 부서 추천은 벡터 검색을 쓰지 않음 (`rag/dept_recommender.py`, `rag/bm25.py`)

임베딩 엔드포인트가 없던 시기에 만든 구성이 유지되고 있습니다(ADR-0027).

1. **BM25 사전점수**: 프로세스 안의 순수 파이썬 BM25(`k1=1.5`, `b=0.75`). 문서 = 팀 업무분장 항목 문장 + 업무보고 요약 불릿. 팀 점수 = `0.7·max + 0.3·상위 3개 평균` → [0, 1] 정규화
2. **후보 축소**: 상위 N팀(`ASM_DEPT_SHORTLIST`) + 원문이 지목한 팀은 반드시 포함
3. **LLM 판정**: 후보 팀마다 병렬로 0~100점 판정(같은 입력은 Redis에 7일 캐시)
4. **최종 점수**: `0.65·판정/100 + 0.35·BM25`, 판정 구간(소관 ≥70 · 관련 40~69) 우선 정렬 후 상위 최대 3팀

---

## 7. 이 PoC 임베딩 정책에 옮길 때 체크리스트

국회대응의 청킹 규칙은 공문 · 보고서 · 답변서에 맞춘 것이라 내부 규정(조 · 항 · 호 구조)에는 그대로 쓰지 않습니다. **임베딩 · BM25 생성 · 컬렉션 운영은 따르고, 청킹과 그에 묶인 값은 규정용으로 새로 정합니다.**

### 7.1 그대로 따를 것

> 반영 현황(2026-10-29): 임베딩 · BM25 생성 · 별칭과 물리 이름 · 앱 RRF는 `ingest/`(`embed.py` · `sparse.py` · `sync.py`)와 `backend/search.py`에 반영했습니다. 리랭커는 아직 쓰지 않습니다([RAG_DESIGN_NOTES.md](RAG_DESIGN_NOTES.md) §7). BM25 `avgdl`은 700 대신 첫 `sync` 실측값을 `RAG_BM25_AVGDL_*`로 고정합니다([DB_LOADING_PLAN.md](DB_LOADING_PLAN.md) §7.6). 아래 체크 상자는 조사 당시 목록이라 따로 갱신하지 않습니다.

**임베딩 · 리랭커**
- [ ] 임베딩 모델 `bge-m3-ko`(1024, COSINE), 접두어 없음, OpenAI 호환 `/v1/embeddings`
- [ ] 임베딩 호출 규칙: 배치 32, 최대 입력 8192토큰(넘으면 앞부분 + 경고), 차원 불일치 시 거부(섞인 벡터 금지)
- [ ] 리랭커 `bge-reranker-v2-m3-ko`, Cohere 호환 `/v1/rerank`, 0~1 점수

**BM25 sparse 생성**
- [ ] 토큰 규칙: 낱말 + 낱말 안 2-gram, 일반 낱말(불용어) 제외, 형태소 분석기 없음
- [ ] 토큰 → 인덱스 `CRC32(UTF-8)`, 문서 값은 BM25 TF 포화(`k1=1.2`, `b=0.75`), 질의 값은 토큰마다 1.0
- [ ] IDF는 Qdrant `modifier=IDF`에 맡김
- [ ] 색인과 질의가 같은 토큰화 함수를 씀

**검색 결합**
- [ ] Hybrid: dense + sparse → 앱 코드 RRF(`k=60`에서 시작) → 리랭크
- [ ] 임베딩 · 리랭커가 없어도 sparse만으로 동작하는 경로 유지

**컬렉션 · 재색인 운영**
- [ ] 별칭 + 물리 이름 `{논리명}__{모델}__{차원}` (dense 없으면 `__bm25__0`)
- [ ] 포인트 ID = `uuid5(doc_id_chunk_idx)` (결정적)
- [ ] 페이로드에 임베딩 모델명 기록
- [ ] 청킹 규칙은 임베딩 모델과 분리해 고정. 모델 교체는 DB 청크로 새 컬렉션 재색인 → 평가 → 별칭 전환
- [ ] 검색 결과는 DB 상태로 한 번 더 거르기, 필터 없는 검색 금지

### 7.2 규정용으로 새로 정할 것

**청킹**
- [ ] 자르는 단위: 조문 기준 또는 parent/child 3계층([RAG_DESIGN_NOTES.md](RAG_DESIGN_NOTES.md) §5.3)
- [ ] 청크 크기 · 겹침: 국회대응의 500/80 대신 조문 길이 분포를 보고 정함
- [ ] 별표 · 서식 · 표 처리 방법
- [ ] 제목 경로 prefix 형식: 예 `[규정명 > 장 > 제n조(제목)]`. 조문 번호 · 조 제목이 BM25에 잡히도록

**청킹에 묶인 값**
- [ ] BM25 평균 문서 길이(`AVG_DOC_TOKENS`, 국회대응 700): 규정 청크의 실제 평균 토큰 수로 다시 정함
- [ ] 리랭커 입력 길이 확인: 리랭크 서버의 최대 길이 설정을 확인하고, 긴 청크라면 child로 검색 · 리랭크하고 parent를 LLM에 넘길지 정함
- [ ] 후보 수 · 최종 수(국회대응 40 / 8)
- [ ] 출처 다양성 상한의 단위: 국회대응의 "장" 대신 "규정" 또는 "조"

**평가로 정할 값**
- [ ] RRF `k`, 리랭크 점수 컷(`RERANK_MIN_SCORE`). 규정 질의 평가 세트로 정하고 모델 교체 시 다시 정함
- [ ] 합의 보존 규칙(모든 질의에서 3위 안 → 최대 2개 보존) 적용 여부

원 프로젝트 안에서 규정검색(regulation)에 적용하는 경우의 제약: 공통 부품(`core/embedding`, `core/rerank`, `core/qdrant`)은 그대로 쓸 수 있지만 `retrieval.py` · `sparse.py`는 `modules/assembly` 안에 있어 **서비스 간 import 금지** 규칙에 걸립니다. 공통 변경 요청으로 공통 쪽에 올리거나 서비스 폴더에 따로 구현해야 하며, 컬렉션 별칭은 `reg_chunks`를 씁니다.

---

## 8. 원 프로젝트 소스 위치

| 내용 | 경로 (`kicox-poc/backend/` 기준) |
| :--- | :--- |
| 검색 파이프라인 | `app/modules/assembly/rag/retrieval.py` |
| sparse 벡터 | `app/modules/assembly/rag/sparse.py` |
| BM25 · 불용어 | `app/modules/assembly/rag/bm25.py` |
| 청킹 | `app/modules/assembly/rag/chunking.py` |
| 색인 재료(페이로드 · 포인트) | `app/modules/assembly/rag/indexing.py` |
| 컬렉션 · 페이로드 키 | `app/modules/assembly/rag/collections.py` |
| 검색 범위 필터 | `app/modules/assembly/rag/filters.py` |
| 질의 계획(세부 질문) | `app/modules/assembly/rag/query_plan.py` |
| 초안 태스크(호출부) | `app/modules/assembly/tasks/draft.py` |
| 부서 추천 | `app/modules/assembly/rag/dept_recommender.py` |
| Qdrant 공통 | `app/core/qdrant.py` |
| 임베딩 공통 | `app/core/embedding/__init__.py`, `providers/openai.py` |
| 리랭크 공통 | `app/core/rerank/__init__.py`, `providers/cohere_v1.py` |
| 설정 | `app/core/settings.py` |
| 재색인 스크립트 | `scripts/reindex_vectors.py` |
| 관련 ADR | 0027(BM25 부서 추천), 0030(임베딩 교체형 · 별칭), 0036(합의 보존), 0039·0040(출처 다양성), 0051(처리 단계 기록), 0057(세부 질문 검색) |
