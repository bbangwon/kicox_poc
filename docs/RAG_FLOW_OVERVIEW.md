# RAG 챗봇 흐름 한눈에 보기

[RAG_DESIGN_NOTES.md](RAG_DESIGN_NOTES.md)의 설계 중 **지금 구현된 흐름**을 그림으로 간단히 정리한 문서입니다. 아직 구현하지 않은 설계(카테고리, 도구 바로 호출 등)는 "미구현"으로 표시합니다. 세부 근거와 수치는 각 그림 아래에 적은 원문 절을 봅니다.

- **기준일**: 2026-10-29
- **상태**: 구현 반영 (`backend/graph.py` 노드 9개: analyze · route · plan · read · tool · ask · resume · answer · no_reference)

---

## 1. 전체 구조

미리 해 두는 **전처리**(오프라인)와 사용자가 질문할 때 도는 **질의 처리**(온라인) 두 부분으로 나뉩니다.

```mermaid
flowchart LR
    subgraph OFF["전처리 (오프라인 배치, kicox_regulation_prep)"]
        PDF["규정 PDF"] --> PRE["파싱 · 청킹 · 요약 · 도구 추출"]
        PRE --> CHK["사람 검증 · 도구 승인<br/>(verify.json)"]
    end

    subgraph STORE["저장소"]
        PG[("PostgreSQL<br/>원본")]
        QD[("Qdrant<br/>검색 인덱스")]
        PG -- "ingest sync" --> QD
    end

    subgraph ON["질의 처리 (온라인)"]
        Q["사용자 질문"] --> QA["analyze<br/>범위 밖 · 재작성"]
        QA --> R["route<br/>문서 고르기"]
        R --> P["plan<br/>청크 고르기"]
        P --> RD["read<br/>원문 + 이웃"]
        RD --> T["tool<br/>계산 (승인 도구가 있을 때)"]
        T --> ANS["answer + 근거<br/>(값이 비면 ask)"]
    end

    CHK -- "ingest load" --> PG
    QD -. 검색 .-> R
    QD -. 검색 .-> P
    PG -. "원문 · 도구 조회" .-> RD
```

---

## 2. 문서를 3계층으로 나누기

PDF 하나를 **문서 요약 → parent → child** 3계층으로 나누고, 계층마다 쓰는 곳이 다릅니다. (원문 §5.3)

```mermaid
flowchart TD
    DOC["문서 요약 (문서당 1개)<br/>summary · keywords · questions · not_for"]
    PA1["parent (2~4K 토큰)<br/>covers · not_covers · keywords · questions"]
    PA2["parent"]
    C1["child (512 토큰 상한)<br/>원문 조각 + 문맥 머리말 + 쪽번호"]
    C2["child"]
    C3["child"]

    DOC --> PA1
    DOC --> PA2
    PA1 --> C1
    PA1 --> C2
    PA2 --> C3

    DOC -. "route가 문서를 고를 때" .- U1(("route"))
    PA1 -. "plan이 청크를 고를 때" .- U2(("plan"))
    C1 -. "read가 원문을 읽을 때" .- U3(("read"))
```

| 계층 | 개수 | 요약 메타데이터 | 쓰는 곳 |
| :--- | :--- | :--- | :--- |
| 문서 요약 | 문서당 1개 | `summary`·`keywords`·`questions`·`not_for` | route (`reg_catalog` 컬렉션) |
| parent | 문서당 여러 개 | `covers`·`not_covers`·`keywords`·`questions` | plan (`reg_chunks` 컬렉션) |
| child | parent당 4~8개 | 없음 (문맥 머리말만 붙임) | 청크 검색, 원문 근거 |

- 요약 메타데이터는 청크마다가 아니라 **문서와 parent에만** 있습니다.
- `not_for`·`not_covers`는 "헷갈리기 쉽지만 여기가 아니라 저기"를 적는 필드입니다 (예: 시내출장 parent의 `not_covers`에 "4시간 이상·시외 일비 → 시외출장"). 검색에는 넣지 않고, 후보가 뽑힌 뒤 LLM이 고를 때만 보여 줍니다.
- 문서 요약은 본문이 32K에 들어가면(약 50쪽 이하) 본문 전체로 한 번에 만들고, 넘으면 parent 요약을 합쳐서 만듭니다.

---

## 3. 전처리 파이프라인

(원문 §5.3.3)

```mermaid
flowchart TD
    A["PDF"] --> B["쪽 이미지를 seed32-think 비전으로<br/>마크다운 옮겨 적기"]
    B --> C["쪽 이어 붙이기<br/>번호 패턴·목차로 제목 수준 정리"]
    C --> D["구조 기반 분할<br/>장·절·항목 경계, 표·단서는 자르지 않음"]
    D --> E["child 목록"]
    E --> F["장·절 경계로 묶기"]
    F --> G["parent 목록"]
    G --> H["parent 요약 (LLM)"]
    H --> I["문서 요약 (LLM)"]
    I --> TL["계산 도구 추출 (LLM)<br/>formula · table_lookup"]
    TL --> J["사람 검증 (verify.json)<br/>수치 포함 child·요약 · 도구 승인"]
    J -- "ingest load" --> K[("PostgreSQL")]
    K -- "ingest sync: 바뀐 행만 다시 임베딩" --> L[("Qdrant")]

    T["PDF 텍스트 레이어"] -. "대조용" .-> J
```

- 비전으로 옮겨 적은 이유: 여비 PDF는 본문 숫자가 텍스트 레이어에 없어서, 텍스트 추출·OCR로는 수치를 살리지 못했습니다(원문 §5.3.5).
- 도구는 승인된 것만 적재합니다(적재 조건은 [RDB_SPEC.md](RDB_SPEC.md) §3.4).

---

## 4. 질의 처리 흐름

(원문 §4, §5)

```mermaid
flowchart TD
    Q["사용자 입력"] --> START{"대기 중인 역질문?<br/>(state.pending)"}
    START -- 예 --> RS["resume<br/>답인지 판정 · 값 채우기"]
    START -- 아니오 --> QA["analyze<br/>범위 밖 판정 · 질의 재작성(1~3개)"]
    RS -- "새 질문" --> QA
    RS -- "답" --> EXE

    QA -- "범위 밖 (인사말, 개인 잔액 등)" --> NONE["no_reference<br/>답변 가능한 규정 목록 + 담당 부서 문의 안내"]
    QA -- "검색용 질의" --> S["route: reg_catalog 하이브리드 검색<br/>(dense + BM25, 앱 RRF) → 후보 최대 20"]
    S --> SEL{"router LLM<br/>문서명 · summary · not_for"}
    SEL -- "해당 없음" --> NONE
    SEL -- "문서 최대 3개" --> PL{"plan<br/>선택 문서 목차(parent covers · not_covers<br/>+ child 머리말, 검색 적중 ★)"}
    PL -- "근거 없음 (1회, 고른 문서 빼고)" --> S
    PL -- "근거 없음 (재시도 뒤)" --> NONE
    PL -- "청크 최대 6개" --> RD["read<br/>child 원문 + 앞뒤 이웃 1개"]
    RD -- "승인 도구 없음" --> ANS
    RD -- "승인 도구 있음" --> TL["tool<br/>LLM이 도구 · 입력값 선택<br/>(모르면 비움, 최대 3호출)"]
    TL --> EXE["실행기(tools_exec)로 계산"]
    EXE -- "빈 입력 없음 · 실행 실패 ·<br/>역질문 2회 · '모른다'" --> ANS["answer<br/>근거 [n] 인용 (원문 · 계산 결과)"]
    EXE -- "빈 입력 (역질문 2회 미만)" --> ASK["ask<br/>되묻고 턴 종료"]
```

| 경로 | 예시 질문 | 흐름 | 근거 표시 |
| :--- | :--- | :--- | :--- |
| 원문 조회 | "복지포인트는 언제까지 써야 해요?" | analyze → route → plan → read → answer | 문서·청크·쪽 |
| 원문 확인 후 도구 | "연차휴가는 며칠이에요?" (복무규정 `annual_leave_days`) | … → read → tool → (ask) → answer | 원문 + 계산 근거 카드(입력값 · 계산 단계 · 근거 문구 · 쪽) |

- 원문을 먼저 읽고, 선택 문서에 승인 도구가 있으면 tool 노드가 계산이 필요한지 판단합니다. 도구는 계산(`formula` · `table_lookup`)만 있고 판정 도구는 없습니다.
- 도구 실행이 실패하면 계산 없이 원문으로만 답합니다. 역질문 2회 뒤나 사용자가 "모른다"고 하면 경우를 나눠 조건부로 답합니다.
- planner가 원문을 건너뛰고 도구를 바로 부르는 경로는 설계(원문 §4.1 단계 3)에만 있고 **미구현**입니다.
- 문서가 적은 지금은 planner에 선택 문서의 목차(parent 요약 + child 머리말 전체, 검색 적중 ★)를 통째로 줍니다(원문 §5.4-5).
- 질의 분석은 이전 턴 맥락을 반영해 검색용 질의를 다시 쓰고("해외는요?" → "해외 출장 일비"), 구어를 규정 용어로 바꾸고, 복합 질문은 나눕니다.
- 카테고리는 **추정만 하고 걸러내지 않는다**는 설계입니다(원문 §4.5). 분류안이 정해지기 전이라 지금은 카테고리를 추정하지 않습니다(**미구현**).
- 한도: 문서 최대 3 · 청크 최대 6 · route 재시도 1회 · 도구 호출 최대 3 · 역질문 최대 2회 (`backend/graph.py`).
- 화면용 SSE 이벤트 목록은 [README.md](../README.md) "SSE 이벤트"를 봅니다.

---

## 5. 역질문 멀티턴

역질문에 대한 답("5년이요")만 보고 다시 라우팅하면 다른 문서로 튈 수 있으므로, 다음 턴은 **저장된 state로 이어서** 처리합니다. (원문 §3-4, §3-5, §7)

```mermaid
sequenceDiagram
    actor U as 사용자
    participant G as analyze·route·plan·read
    participant T as tool
    participant X as 실행기(tools_exec)
    participant A as ask
    participant R as resume
    participant S as state

    U->>G: 연차휴가는 며칠이에요?
    G->>T: 선택 문서 원문 · 승인 도구 (복무규정)
    T->>X: annual_leave_days (계속근무년수 = 비움)
    X-->>A: 빈 입력: 계속근무년수
    A->>S: 대기 중인 호출 저장 (pending)
    A-->>U: 계산하려면 다음 정보가 필요합니다 (계속근무년수)

    U->>R: 5년이요
    S-->>R: 대기 중인 호출 불러오기
    Note over R: 답인지 판정 (새 질문이면 analyze부터)
    R->>X: annual_leave_days (계속근무년수 = 5)
    X-->>R: 결과
    R-->>U: answer: 답변 + 근거 (원문 · 계산 결과)
```

- LLM은 사용자가 말한 값과 근거에서 바로 정해지는 값만 넣고 모르는 값은 비워 둡니다. 빈 입력을 찾아 역질문으로 넘기는 것은 코드(`_run_calls`)가 합니다. 값을 추측해 채우지 않습니다.
- 대기 중인 도구 호출과 입력값은 state(`tool_calls` · `tool_missing` · `asks` · `pending`)에 남고, 새 질문이 오면 비웁니다. 앞 턴에서 말한 값은 tool 노드가 최근 대화(6개)에서 다시 읽습니다.
- 대화 state는 지금 메모리(`InMemorySaver`)에 있어 서버를 다시 띄우면 대기 중인 역질문이 사라집니다.

---

## 6. 저장소 역할

(원문 §5.2, 구현 명세는 [RDB_SPEC.md](RDB_SPEC.md) · [VDB_SPEC.md](VDB_SPEC.md))

```mermaid
flowchart LR
    subgraph PG["PostgreSQL (원본)"]
        D1["documents<br/>문서 판 · 문서 요약"]
        D2["sections<br/>parent + 요약 필드"]
        D3["chunks<br/>child 본문 · 쪽번호"]
        D4["tools · document_tools<br/>승인 도구 · 문서↔도구 N:M"]
        D1 --> D2 --> D3
    end

    subgraph QD["Qdrant (검색 인덱스)"]
        C1["reg_catalog<br/>summary 1개 + 예상 질문마다 1개<br/>→ doc_id로 묶어 문서 후보"]
        C2["reg_chunks<br/>child + parent 요약 (kind별 top-k)<br/>→ section_id로 묶어 표시"]
    end

    D1 -- 동기화 --> C1
    D2 -- 동기화 --> C2
    D3 -- 동기화 --> C2
```

- PostgreSQL이 원본이고 Qdrant는 원본에서 다시 만들 수 있는 인덱스입니다. 사람의 검증 · 승인은 전처리 뷰어(`verify.json`)에서 하고 다시 적재합니다.
- 검색은 dense(`bge-m3-ko`)와 sparse(낱말 + 한글 2-gram, CRC32 BM25)를 앱에서 RRF(k=60)로 합칩니다. 리랭커는 아직 쓰지 않습니다.
