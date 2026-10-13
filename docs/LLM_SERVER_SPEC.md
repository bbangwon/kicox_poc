# LLM 서버 사양 및 연동 가이드 (LLM Server Specification & Integration Guide)

본 문서는 사내 로컬 LLM 서버 인프라 정보와 호스팅 모델의 성능 분석 결과, 그리고 향후 LLM 응용 소프트웨어 개발 시 필요한 API 연동 가이드를 정리한 문서입니다.

---

## 1. 서버 접속 및 인프라 개요

* **서버 호스트**: `192.168.0.58`
* **서비스 포트**: `11434` (기본 Ollama 서비스 포트)
* **네이티브 Base URL**: `http://192.168.0.58:11434`
* **OpenAI 호환 Base URL**: `http://192.168.0.58:11434/v1`
* **네트워크 상태**: 내부망 정상 접근 가능 (HTTP 200)
* **인증(Auth)**: 내부망 호출 시 기본 인증 불필요 (OpenAI SDK 연동 시 dummy api-key 사용 가능)

---

## 2. 호스팅 모델 사양 및 비교

서버의 `/api/tags` 및 실제 벤치마크 테스트를 통해 추출한 모델 상세 사양입니다.

| 항목 | `gpt-oss:latest` | `qwen3.6:35b` | `gemma4:31b` |
| :--- | :--- | :--- | :--- |
| **모델 파라미터** | 20.9B | 35.5B (MoE 아키텍처) | 31.3B (Dense 아키텍처) |
| **양자화 (Quantization)** | `MXFP4` | `Q4_K_M` | `Q4_K_M` |
| **디스크/VRAM 크기** | 약 13.8 GB | 약 22.6 GB | 약 19.9 GB |
| **컨텍스트 길이 (Context Window)** | 131,072 (128K) | 262,144 (256K) | 262,144 (256K) |
| **임베딩 차원** | 2,880 | 2,048 | 5,376 |
| **모델 패밀리** | `gptoss` | `qwen35moe` | `gemma4` |
| **추론 속도 (eval speed)** | **약 59.1 tokens/s** | **약 77.9 tokens/s** *(가장 빠름)* | **약 10.0 tokens/s** |
| **모델 로딩 시간 (Cold Start)** | ~5.1초 | ~5.8초 | ~9.2초 |
| **지원 기능 (Capabilities)** | Completion, Tools, Thinking | Completion, Vision, Tools, Thinking *(MCP 최적)* | Completion, Vision, Tools, Thinking |

> **추천 모델 활용 가이드:**
> * **일반적인 백엔드 서비스 및 실시간 API**: **`qwen3.6:35b`** 추천 (가장 빠른 응답 속도인 ~78 tps, 긴 컨텍스트 256K, MoE 기반 고효율).
> * **MCP(Model Context Protocol) 및 Function Calling**: **`qwen3.6:35b`** 적극 권장. JSON 스키마 이해도와 Tool Calling 규격 준수 능력이 가장 뛰어나며, 256K 컨텍스트로 다양한 MCP 도구 주입에 유리합니다.
> * **복잡한 도구 호출 및 심층 추론(Thinking)**: **`gpt-oss:latest`** 또는 **`qwen3.6:35b`**.
> * **시각 분석(Vision) / 멀티모달 처리**: **`qwen3.6:35b`** 또는 **`gemma4:31b`**.

---

## 3. 모델별 테스트 결과 상세 (2026-10-12)

동일한 프롬프트(`"갈비찜 레시피 알려줘"`, `stream: false`)로 진행한 테스트 결과입니다.

### 3.1. `gpt-oss:latest`
* **총 소요 시간**: 44.1초
* **모델 로드 시간**: 5.14초
* **토큰 생성 수**: 2,285 토큰 (Thinking 토큰 포함)
* **생성 속도**: 59.1 tokens/s
* **결과 특징**:
  * 내부 사고(Thinking) 과정을 거친 후 한국어 마크다운 표와 상세한 조리 단계, 분량, 꿀팁까지 매우 풍부하게 작성.
  * 지시 이행 능력과 구조화된 출력 품질이 매우 높음.

### 3.2. `qwen3.6:35b`
* **총 소요 시간**: 22.9초
* **모델 로드 시간**: 5.76초
* **토큰 생성 수**: 1,329 토큰
* **생성 속도**: 77.9 tokens/s
* **결과 특징**:
  * 35B 급임에도 MoE 아키텍처 특성 덕분에 3개 모델 중 가장 빠른 토큰 생성 속도를 보임.
  * 2인분 기준 계량, 이모지 활용, 성공 요령(TIP) 등 실용적이고 직관적인 한국어 응답을 간결하게 구성함.

### 3.3. `gemma4:31b`
* **총 소요 시간**: 156.5초 (~2분 36초)
* **모델 로드 시간**: 9.15초
* **토큰 생성 수**: 1,465 토큰
* **생성 속도**: 10.0 tokens/s
* **결과 특징**:
  * Dense 31B 모델 특성상 연산량이 집중되어 단일 스레드/GPU 추론 속도가 상대적으로 느린 편(~10 tps).
  * 배 음료를 활용한 연육 팁, 채소 모서리 둥글게 깎기 등 한국 가정식 조리에 대한 도메인 이해도가 매우 뛰어남.
### 3.4. 한국어 AI 에이전트 MCP 오케스트레이션 실증 테스트 (`qwen3.6:35b`)

복수 도구(문서 검색, 수치 계산, 이메일 발송)가 정의된 환경에서 복합 한국어 지시를 처리하는 3단계 자율 에이전트 루프를 검증한 결과입니다.

* **사용자 지시**: *"지난 분기 신규 가입자 수가 어떻게 되는지 사내 문서에서 찾아서, 그 수치에 10% 증가분을 계산한 뒤 운영팀(ops@company.com)으로 보고 이메일을 보내줘."*
* **오케스트레이션 실행 흐름**:
  1. **1단계 (사내 문서 검색 도구 호출)**:
     * 내부 사고(Thinking) 후 한국어 키워드로 `search_internal_docs(query="지난 분기 신규 가입자 수")` 호출 (약 2.8초)
  2. **2단계 (수치 계산 도구 호출)**:
     * 문서 검색 결과("50,000명")를 분석하여 즉시 `calculate(expression="50000 * 1.1")` 호출 (약 0.74초)
  3. **3단계 (이메일 발송 도구 호출 & 한국어 본문 작성)**:
     * 계산 결과("55,000명")와 이전 문맥을 취합하여 비즈니스 한국어 서식의 이메일 초안 작성 후 `send_email(recipient="ops@company.com", subject=..., body=...)` 호출 (약 5.4초)
* **결론**: 도구 간의 전후 의존 관계 파악, 한국어 인자 추출 및 비즈니스 작문까지 완벽하게 수행됨.

---

## 4. API 연동 규격

### 4.1. Ollama 네이티브 API (`/api/chat`)

#### 엔드포인트
`POST http://192.168.0.58:11434/api/chat`

#### Headers
```http
Content-Type: application/json
```

#### Request Body
```json
{
  "model": "qwen3.6:35b",
  "messages": [
    {
      "role": "system",
      "content": "당신은 요리 전문가 AI입니다."
    },
    {
      "role": "user",
      "content": "갈비찜 레시피 알려줘"
    }
  ],
  "stream": false,
  "options": {
    "temperature": 0.7,
    "top_p": 0.9
  }
}
```

#### Response Body 주요 필드
```json
{
  "model": "qwen3.6:35b",
  "created_at": "2026-10-12T08:26:03.34136311Z",
  "message": {
    "role": "assistant",
    "content": "..."
  },
  "done": true,
  "done_reason": "stop",
  "total_duration": 22952293819,
  "load_duration": 5763065071,
  "eval_count": 1329,
  "eval_duration": 17046846000
}
```

---

### 4.2. OpenAI 호환 API (`/v1/chat/completions`)

기존 OpenAI 라이브러리, LangChain, LlamaIndex, LiteLLM 등의 프레임워크와 직접 호환됩니다.

#### 엔드포인트
`POST http://192.168.0.58:11434/v1/chat/completions`

#### Request Body
```json
{
  "model": "qwen3.6:35b",
  "messages": [
    {"role": "user", "content": "안녕하세요!"}
  ],
  "temperature": 0.7,
  "stream": false
}
```

---

## 5. 소프트웨어 개발용 코드 예제

### 5.1. Python (공식 `openai` SDK 활용) - **가장 권장**

```python
from openai import OpenAI

# 로컬 LLM 서버를 가리키도록 base_url 설정 (api_key는 더미값 지정)
client = OpenAI(
    base_url="http://192.168.0.58:11434/v1",
    api_key="ollama",  # 필수값 형태이나 인증 우회됨
)

def chat_example():
    response = client.chat.completions.create(
        model="qwen3.6:35b",
        messages=[
            {"role": "system", "content": "당신은 친절한 AI 비서입니다."},
            {"role": "user", "content": "갈비찜 레시피 알려줘"},
        ],
        temperature=0.7,
        stream=True,  # 실시간 사용자 경험을 위해 스트리밍 권장
    )

    for chunk in response:
        content = chunk.choices[0].delta.content
        if content:
            print(content, end="", flush=True)

if __name__ == "__main__":
    chat_example()
```

### 5.2. Python (`httpx` 비동기 네이티브 API 호출)

```python
import httpx
import asyncio

async def call_ollama(prompt: str, model: str = "qwen3.6:35b"):
    url = "http://192.168.0.58:11434/api/chat"
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
    }

    async with httpx.AsyncClient(timeout=120.0) as client:
        response = await client.post(url, json=payload)
        response.raise_for_status()
        data = response.json()
        return data["message"]["content"]

if __name__ == "__main__":
    reply = asyncio.run(call_ollama("갈비찜 레시피 알려줘"))
    print(reply)
```

### 5.3. LangChain 연동 예제

```python
from langchain_community.chat_models import ChatOllama
from langchain_core.messages import HumanMessage

llm = ChatOllama(
    base_url="http://192.168.0.58:11434",
    model="qwen3.6:35b",
    temperature=0.7,
)

messages = [HumanMessage(content="갈비찜 레시피 요약해줘")]
response = llm.invoke(messages)
print(response.content)
```

---

## 6. 개발 시 고려사항 및 베스트 프랙티스

1. **스트리밍(`stream: true`) 필수 고려**:
   * 로컬 GPU 환경에서 모델이 긴 텍스트를 생성할 때 `stream: false`는 클라이언트 입장에서 수십 초간 응답 대기(Blocking)가 발생합니다.
   * UI/UX 구축 시 스트리밍 방식으로 청크(chunk) 단위로 받아 렌더링하는 것을 권장합니다.
2. **타임아웃(Timeout) 설정**:
   * 모델 최초 로딩(Cold Start, 5~10초) 및 장문 생성 시 일반 HTTP 클라이언트 기본 타임아웃(보통 10~30초)에 걸려 `ReadTimeout`이 발생할 수 있습니다.
   * 클라이언트 타임아웃을 최소 **60초 ~ 180초** 이상으로 설정하십시오.
3. **GPU 메모리 스왑 오버헤드**:
   * 한 번에 하나의 모델만 VRAM에 상주하는 경우, `qwen3.6:35b` $\leftrightarrow$ `gemma4:31b` 등 모델 전환 시 모델 가중치 재로드(5~10초) 오버헤드가 발생할 수 있습니다. 주력 모델(예: `qwen3.6:35b`)을 정해두고 통일하는 것이 서비스 속도에 유리합니다.
4. **한국어 지원 품질**:
   * 테스트한 3개 모델 모두 한국어 이해 및 조리법 생성 품질이 우수하였으나, 처리 속도와 자원 효율 측면에서는 **`qwen3.6:35b`**가 가장 우수한 성능을 보였습니다.
