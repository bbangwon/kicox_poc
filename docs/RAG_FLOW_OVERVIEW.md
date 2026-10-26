# RAG 챗봇 흐름 한눈에 보기

[RAG_DESIGN_NOTES.md](RAG_DESIGN_NOTES.md)에서 확정한 설계를 그림으로 간단히 정리한 문서입니다. 세부 근거와 수치는 각 그림 아래에 적은 원문 절을 봅니다.

- **기준일**: 2026-10-26
- **상태**: 설계 확정, 구현 전

---

## 1. 전체 구조

미리 해 두는 **전처리**(오프라인)와 사용자가 질문할 때 도는 **질의 처리**(온라인) 두 부분으로 나뉩니다.

```mermaid
flowchart LR
    subgraph OFF["전처리 (오프라인 배치)"]
        PDF["규정 PDF"] --> PRE["파싱 · 청킹 · 요약"]
        PRE --> CHK["사람 검증<br/>(수치 포함 부분만)"]
    end

    subgraph STORE["저장소"]
        PG[("PostgreSQL<br/>원본")]
        QD[("Qdrant<br/>검색 인덱스")]
        PG -- 동기화 --> QD
    end

    subgraph ON["질의 처리 (온라인)"]
        Q["사용자 질문"] --> R["router<br/>문서 고르기"]
        R --> P["planner<br/>청크·도구 계획"]
        P --> A["agent<br/>원문 조회 · 도구 호출"]
        A --> ANS["답변 + 근거"]
    end

    CHK --> PG
    QD -. 검색 .-> R
    QD -. 검색 .-> P
    PG -. 원문·도구 조회 .-> A
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

    DOC -. "router가 문서를 고를 때" .- U1(("router"))
    PA1 -. "planner가 청크를 고를 때" .- U2(("planner"))
    C1 -. "agent가 원문을 읽을 때" .- U3(("agent"))
```

| 계층 | 개수 | 요약 메타데이터 | 쓰는 곳 |
| :--- | :--- | :--- | :--- |
| 문서 요약 | 문서당 1개 | `summary`·`keywords`·`questions`·`not_for` | router (`doc_catalog` 컬렉션) |
| parent | 문서당 여러 개 | `covers`·`not_covers`·`keywords`·`questions` | planner (`chunks` 컬렉션) |
| child | parent당 4~8개 | 없음 (문맥 머리말만 붙임) | 청크 검색, 원문 근거 |

- 요약 메타데이터는 청크마다가 아니라 **문서와 parent에만** 있습니다.
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
    I --> J["사람 검증<br/>수치 포함 child·요약만"]
    J --> K[("PostgreSQL")]
    K -- "바뀐 행만 다시 임베딩" --> L[("Qdrant")]

    T["PDF 텍스트 레이어"] -. "대조용" .-> J
```

- 비전으로 옮겨 적은 이유: 여비 PDF는 본문 숫자가 텍스트 레이어에 없어서, 텍스트 추출·OCR로는 수치를 살리지 못했습니다(원문 §5.3.5).

---

## 4. 질의 처리 흐름

(원문 §4, §5)

```mermaid
flowchart TD
    Q["사용자 질문"] --> OOS{"처음부터<br/>범위 밖?"}
    OOS -- "예 (인사말, 개인 잔액 등)" --> NONE["참조문서 없음<br/>→ 담당 부서 안내"]
    OOS -- 아니오 --> S["문서 후보 검색<br/>BM25 + 벡터 하이브리드<br/>→ 후보 10~20개"]

    S --> SEL{"router LLM<br/>카테고리 목록 + 후보를 보고 선택"}
    SEL -- "후보가 안 맞음" --> CAT["카테고리 문서 목록 펼치기"]
    CAT --> SEL
    SEL -- "해당 없음" --> NONE
    SEL -- "문서 최대 3개" --> ST[("state 저장<br/>선택 문서 · 도구")]

    ST --> PL{"planner<br/>선택 문서의 parent 요약 목록<br/>+ 문서별 도구를 보고 경로 결정"}
    PL -- "계산·판정 질문" --> CALL["문서별 도구 호출"]
    PL -- "규정 내용 질문" --> AG["agent<br/>child 원문 조회 (앞뒤 이웃 포함)"]
    PL -- "조건 확인 후 계산" --> AG

    AG --> EV{"근거 충분?"}
    EV -- "부족 (1회까지)" --> SEL
    EV -- "끝까지 부족" --> NONE
    EV -- "충분, 계산 필요" --> CALL
    EV -- "충분" --> ANS["답변<br/>+ 근거 (문서·청크·쪽 / 도구 basis)"]

    CALL --> NEED{"정보 부족?<br/>(도구가 need_info로 판정)"}
    NEED -- 아니오 --> ANS
    NEED -- "예 (최대 2회)" --> ASK["사용자에게 역질문"]
    NEED -- "2회 넘음" --> COND["경우를 나눠<br/>조건부로 답변"]
```

planner는 원문을 먼저 읽을지, 도구를 바로 부를지를 질문에 따라 정합니다.

| 경로 | 예시 질문 | 흐름 | 근거 표시 |
| :--- | :--- | :--- | :--- |
| 도구 바로 호출 | "시외 출장 3시간이면 일비 얼마예요?" | planner → 도구 | 도구 결과의 `basis` |
| 원문 조회 | "출장 증빙서류는 뭐가 필요해요?" | planner → child 원문 → 답변 | 문서·청크·쪽 |
| 원문 확인 후 도구 | "토익 학원비도 교육훈련비로 받을 수 있고, 학점은 몇 점이에요?" | planner → child 원문(대상 여부) → 도구(학점) | 둘 다 |

- 도구는 규정 수치를 이미 갖고 있으므로, 계산·판정 질문은 원문 조회를 거치지 않습니다. 원문 조회는 도구가 다루지 않는 내용(대상 여부, 예외, 절차)을 확인할 때만 합니다.
- 문서가 5종 미만인 지금 PoC 규모에서는 검색 없이 문서·청크 목차를 한 번에 주고 고르게 합니다(원문 §3-3).
- router는 planner와 분리된 별도 노드입니다.

---

## 5. 역질문 멀티턴

역질문에 대한 답("3시간이요")만 보고 다시 라우팅하면 다른 문서로 튈 수 있으므로, 다음 턴은 **저장된 state로 이어서** 처리합니다. (원문 §3-4, §3-5)

```mermaid
sequenceDiagram
    actor U as 사용자
    participant R as router
    participant G as planner·agent
    participant T as 문서별 도구
    participant S as state

    U->>R: 시외 출장 일비 얼마예요?
    R->>S: 선택 문서·도구 저장 (여비)
    R->>G: 계획·실행
    G->>T: 일비 계산 호출
    T-->>G: need_info (출장 시간)
    G-->>U: 출장 시간이 몇 시간인가요?

    U->>G: 3시간이요
    Note over R,G: 다시 라우팅하지 않음
    S-->>G: 선택 문서·도구·확인된 값 불러오기
    G->>T: 일비 계산 호출 (시간 = 3)
    T-->>G: 결과
    G-->>U: 답변 + 근거 (문서·쪽)
```

- 정보가 부족한지는 LLM이 아니라 **도구가 판정**합니다. LLM은 추측한 값으로 인자를 채우지 않습니다.
- 확인된 값은 state에 쌓고, 주제가 바뀌면 비웁니다.

---

## 6. 저장소 역할

(원문 §5.2)

```mermaid
flowchart LR
    subgraph PG["PostgreSQL (원본)"]
        D1["documents<br/>문서 요약"]
        D2["sections<br/>parent + 요약 필드"]
        D3["chunks<br/>child 본문 · 쪽번호"]
        D4["tools · categories<br/>문서↔도구 · 문서↔카테고리 N:M"]
        D1 --> D2 --> D3
    end

    subgraph QD["Qdrant (검색 인덱스)"]
        C1["doc_catalog<br/>summary 1개 + 예상 질문마다 1개<br/>→ doc_id로 묶어 문서 후보"]
        C2["chunks<br/>child + parent 요약<br/>→ parent_id로 묶어 표시"]
    end

    D1 -- 동기화 --> C1
    D2 -- 동기화 --> C2
    D3 -- 동기화 --> C2
```

- PostgreSQL이 원본이고, 사람은 여기서만 검증·수정합니다. Qdrant는 원본에서 다시 만들 수 있는 인덱스이며 동기화 작업만 씁니다.
- 검색은 dense(`dragonkue/bge-m3-ko`)와 sparse(Kiwi 형태소 + BM25)를 RRF로 합칩니다.
