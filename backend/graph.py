"""LangGraph 오케스트레이션: planner → agent ⇄ tools.

각 노드는 `get_stream_writer()`로 시각화용 커스텀 이벤트를 내보낸다.
이벤트 스키마는 `type` 필드로 구분한다 (README 참고).
"""

import asyncio
import json
import re
import time
from collections.abc import Awaitable, Callable
from functools import wraps
from typing import Annotated, Literal

import httpx
import openai
from langchain_core.messages import AIMessage, AnyMessage, SystemMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool
from langchain_mcp_adapters.client import MultiServerMCPClient
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from typing_extensions import TypedDict

from backend import config as settings
from backend.llm import ChatVLLM


class AgentState(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]
    plan: list[str]


def _llm(thinking: bool) -> ChatVLLM:
    if not settings.LLM_BASE_URL:
        raise RuntimeError("LLM_BASE_URL이 설정되지 않았습니다 (worker-ai-endpoints.env 또는 환경변수)")
    return ChatVLLM(
        base_url=settings.LLM_BASE_URL,
        api_key=settings.LLM_API_KEY,
        model=settings.MODEL,
        temperature=settings.TEMPERATURE,
        max_tokens=settings.MAX_TOKENS,
        timeout=settings.LLM_TIMEOUT,
        max_retries=0,  # 재시도는 _with_retry가 담당한다 (llm_retry 이벤트 발행)
        stream_usage=True,
        extra_body={"chat_template_kwargs": {"thinking": thinking}},
    )


def _emit(event_type: str, **data) -> None:
    get_stream_writer()({"type": event_type, "ts": time.time(), **data})


def _traced(name: str):
    """노드 시작/종료 이벤트와 소요 시간을 내보내는 데코레이터."""

    def deco(fn):
        @wraps(fn)
        async def wrapper(state: AgentState, config: RunnableConfig):
            _emit("node_start", node=name)
            started = time.perf_counter()
            try:
                return await fn(state, config)
            finally:
                _emit("node_end", node=name, ms=round((time.perf_counter() - started) * 1000))

        return wrapper

    return deco


def _text(content) -> str:
    if isinstance(content, str):
        return content
    return "\n".join(b.get("text", "") for b in content if isinstance(b, dict))


def _llm_stats(msg: AIMessage, ms: int) -> dict:
    usage = msg.usage_metadata or {}
    output_tokens = usage.get("output_tokens")
    return {
        "ms": ms,
        "input_tokens": usage.get("input_tokens"),
        "output_tokens": output_tokens,
        # vLLM은 생성 시간을 따로 주지 않으므로 첫 토큰 대기 시간을 포함한 전체 시간 기준이다.
        "tps": round(output_tokens / (ms / 1000), 1) if output_tokens and ms else None,
        # "length"면 max_tokens에서 잘린 응답이다
        "finish_reason": (msg.response_metadata or {}).get("finish_reason"),
    }


def _retryable(e: Exception) -> bool:
    """서버 측 일시 오류만 재시도한다. 4xx(모델 없음 등)는 재시도해도 소용없으므로 제외."""
    if isinstance(e, openai.APIStatusError):
        return e.status_code >= 500
    return isinstance(e, (openai.APIConnectionError, ConnectionError, httpx.TransportError))


async def _with_retry[T](node: str, call: Callable[[], Awaitable[T]]) -> T:
    for attempt in range(settings.LLM_MAX_RETRIES + 1):
        try:
            return await call()
        except Exception as e:
            if attempt == settings.LLM_MAX_RETRIES or not _retryable(e):
                raise
            delay = settings.LLM_RETRY_DELAY * (attempt + 1)
            _emit(
                "llm_retry",
                node=node,
                attempt=attempt + 1,
                max_retries=settings.LLM_MAX_RETRIES,
                delay=delay,
                reason=str(e)[:300],
            )
            await asyncio.sleep(delay)
    raise AssertionError("unreachable")


def _parse_steps(text: str) -> list[str]:
    """{"steps": [...]} 응답에서 단계를 꺼낸다. 닫는 괄호가 없거나 잘린 응답이면 완성된 문자열만 쓴다."""
    try:
        return [str(s) for s in json.loads(text).get("steps", [])]
    except (json.JSONDecodeError, AttributeError):
        pass
    _, sep, rest = text.partition('"steps"')
    return [json.loads(f'"{m}"') for m in re.findall(r'"((?:[^"\\]|\\.)*)"', rest)] if sep else []


PLANNER_PROMPT = """당신은 한국산업단지공단 내부규정 Q&A 에이전트의 플래너입니다.
사용자의 마지막 메시지에 답하기 위한 실행 계획을 짧은 한국어 단계로 작성하세요.
마지막 메시지가 직전 역질문에 대한 답변이면, 이전 대화의 원래 질문과 합쳐서 계획을 세웁니다.
{scope}
사용 가능한 도구:
{tools}

규칙:
- 각 단계는 "도구명(조회 항목): 목적" 또는 "사용자에게 역질문: 무엇을" 또는 "직접 답변: 이유" 형식의 한 문장입니다.
- 답변 범위 밖의 질문, 규정에 없는 내용(금액·한도·잔액 등), 인사말은 단계 하나("직접 답변: …")만 작성합니다.
- 최대 4단계. 반드시 JSON으로만 응답: {{"steps": ["...", "..."]}}"""

SCOPE = """답변 범위: 개인교육훈련비 지원 기준(인사팀) — 지원대상·제외대상, 과세 여부, 교육신청 경로(MIS), 학점구분·직무구분, 이수학점.
범위 밖: 여비(출장비·숙박비·자가용 운임·국외출장·부임여비 등)를 포함한 그 밖의 규정, 개인별 사용액·잔액 조회.
담당 부서: 개인교육훈련비(지원 금액·한도·개인별 잔액 포함)는 인사팀, 여비는 상생복지팀."""

AGENT_PROMPT = """당신은 한국산업단지공단 직원의 내부규정 질문에 답하는 한국어 상담 에이전트입니다.
{scope}

## 근거 원칙
- 규정 원문(get_training_policy 결과)과 도구 결과에 있는 내용만 말하세요. 기억이나 일반 상식으로 규정을 보충하지 마세요.
- 규정에 없는 내용(교육훈련비 지원 금액·한도·잔액 등)은 추측하지 말고 "규정에 명시되어 있지 않아 인사팀 확인이 필요합니다"라고 안내하세요.
  사원번호 등을 물어 조회해 주겠다고 하지 마세요. 당신은 개인 데이터를 조회할 수 없습니다.
- 범위 밖 질문은 도구를 호출하지 말고, 지금은 답변할 수 없다는 것과 위 담당 부서를 안내하세요. 금액을 추정하지 마세요.
- 학점 등 수치는 직접 계산하지 말고 도구 결과의 값을 그대로 쓰세요.

## 지원범위 읽는 법
- [지원대상]의 "직무 / 비과세", "공통 / 과세", "임금피크 / 과세"는 모두 **지원되는** 항목입니다. "과세"는 지원은 되지만 지원금이 과세된다는 뜻입니다.
- 지원 불가는 [제외대상]에 해당하거나, 지원대상 항목의 단서(예: 취업·이직준비 목적 제외)에 걸리는 경우입니다.
- 교육 내용이 현직무·IDP 희망직무와 관련 있는지에 따라 구분(직무/공통)과 과세가 달라지는데 대화에서 알 수 없으면, 추측하지 말고 업무 연관성을 되물으세요.

## 도구 사용
- 지원 여부·과세 질문: get_training_policy(section="지원범위,제외대상")로 원문을 확인한 뒤 답하세요.
- 신청 경로·학점구분·직무구분 질문: get_training_policy로 해당 항목(신청방법·학점구분·직무구분)을 확인한 뒤 답하세요.
- 이수학점 질문: calculate_training_credits를 바로 호출하세요 (도구에 학점 기준이 들어 있으므로 규정 조회가 필요 없습니다).
  사용자가 말하지 않은 인자는 추측하지 말고 생략하세요. 어학·엑셀·OA 교육이면 credit_category="공통"을 넘기세요.

## 역질문(멀티턴)
- 도구가 status="need_info"를 반환하면 questions의 내용을 사용자에게 묻고 답변을 마치세요.
- 질문은 한 번에 모아 짧은 번호 목록으로 하고, 가능하면 선택지를 함께 제시하세요.
- 사용자가 답하면 이전 대화의 원래 질문과 합쳐서, 대화 전체에서 확인된 값을 모두 인자로 넘겨 다시 호출하세요. 이미 답한 것을 다시 묻지 마세요.

## 답변 형식
- 첫 문장에 결론을 쓰세요. (예: "지원됩니다. 다만 공통 교육이라 과세됩니다.", "1학점이 인정됩니다.")
- 이어서 근거와 조건을 2~4개의 짧은 글머리표로 쓰세요. 결과에 conditions가 있으면 "~인 경우에 한해 인정"처럼 함께 안내하세요.
- 규정을 근거로 답했으면 마지막 줄에 "근거: 개인교육훈련비 지원 기준 › <항목명>" 형식으로 표시하세요.
  get_training_policy 결과 끝의 [근거 표기] 줄을 그대로 옮기고, 이수학점 계산이면 "개인교육훈련비 지원 기준 › 이수학점"을 씁니다.
  역질문이나 범위 밖 안내에는 쓰지 않습니다.
- 도구 이름, "규정을 조회하겠습니다" 같은 처리 과정, <cite> 같은 태그나 HTML은 쓰지 마세요.
- "추가 질문이 있으면…", "다른 ~도 도와드릴 수 있습니다" 같은 맺음말로 끝내지 마세요. 근거 줄(또는 안내 문장)에서 답변을 끝냅니다.

## 실행 계획 (참고용 — 위 규칙이 우선합니다)
{plan}"""


def build_graph(tools: list[BaseTool], tool_servers: dict[str, str]):
    tools_by_name = {t.name: t for t in tools}
    tool_catalog = "\n".join(f"- {t.name}: {t.description.splitlines()[0]}" for t in tools)

    @_traced("planner")
    async def planner(state: AgentState, config: RunnableConfig):
        # JSON 모드(response_format)는 쓰지 않는다. seed32-think에서 가끔 끝나지 않고 상한까지 생성한다.
        # 계획은 짧으므로 상한을 작게 두고, 잘리거나 괄호가 빠져도 _parse_steps가 완성된 단계만 꺼낸다.
        llm = _llm(thinking=False).bind(max_tokens=settings.PLANNER_MAX_TOKENS)
        started = time.perf_counter()
        msg = await _with_retry(
            "planner",
            lambda: llm.ainvoke([SystemMessage(PLANNER_PROMPT.format(scope=SCOPE, tools=tool_catalog)), *state["messages"]]),
        )
        steps = _parse_steps(_text(msg.content)) or ["직접 답변"]
        _emit("plan", steps=steps, stats=_llm_stats(msg, round((time.perf_counter() - started) * 1000)))
        return {"plan": steps}

    @_traced("agent")
    async def agent(state: AgentState, config: RunnableConfig):
        thinking = config["configurable"].get("thinking", True)
        llm = _llm(thinking).bind_tools(tools)
        plan = "\n".join(f"{i}. {s}" for i, s in enumerate(state.get("plan", []), 1))
        messages = [SystemMessage(AGENT_PROMPT.format(scope=SCOPE, plan=plan)), *state["messages"]]

        async def stream_once():
            # 재시도 시 이전 시도의 부분 출력은 버린다 (프론트엔드도 llm_retry 수신 시 초기화)
            full = None
            async for chunk in llm.astream(messages):
                if reasoning := chunk.additional_kwargs.get("reasoning_content"):
                    _emit("thinking", delta=reasoning)
                if delta := _text(chunk.content):
                    _emit("token", delta=delta)
                full = chunk if full is None else full + chunk
            return full

        started = time.perf_counter()
        full = await _with_retry("agent", stream_once)

        # additional_kwargs의 reasoning_content(Thinking)는 기록용으로 유지하지만 다음 호출에는 보내지 않는다.
        # seed32-think(vLLM)는 이를 다시 보내면 Thinking이 content로 새어 나온다 (backend/llm.py 참고).
        ai = AIMessage(
            content=full.content,
            additional_kwargs=full.additional_kwargs,
            tool_calls=full.tool_calls,
            response_metadata=full.response_metadata,
            usage_metadata=full.usage_metadata,
            id=full.id,
        )
        _emit(
            "llm_end",
            tool_calls=[{"id": tc["id"], "name": tc["name"]} for tc in ai.tool_calls],
            stats=_llm_stats(ai, round((time.perf_counter() - started) * 1000)),
        )
        return {"messages": [ai]}

    @_traced("tools")
    async def run_tools(state: AgentState, config: RunnableConfig):
        results = []
        for call in state["messages"][-1].tool_calls:
            server = tool_servers.get(call["name"], "?")
            _emit("tool_call", id=call["id"], name=call["name"], server=server, args=call["args"])
            started = time.perf_counter()
            tool = tools_by_name.get(call["name"])
            if tool is None:
                result = ToolMessage(f"알 수 없는 도구: {call['name']}", tool_call_id=call["id"], status="error")
            else:
                try:
                    result = await tool.ainvoke({**call, "type": "tool_call"})
                except Exception as e:  # MCP 전송 오류 등도 모델에 돌려줘 스스로 복구하게 한다
                    result = ToolMessage(f"도구 실행 오류: {e}", tool_call_id=call["id"], status="error")
            _emit(
                "tool_result",
                id=call["id"],
                name=call["name"],
                server=server,
                content=_text(result.content),
                error=result.status == "error",
                ms=round((time.perf_counter() - started) * 1000),
            )
            results.append(result)
        return {"messages": results}

    def route(state: AgentState) -> Literal["tools", "__end__"]:
        return "tools" if state["messages"][-1].tool_calls else END

    g = StateGraph(AgentState)
    g.add_node("planner", planner)
    g.add_node("agent", agent)
    g.add_node("tools", run_tools)
    g.add_edge(START, "planner")
    g.add_edge("planner", "agent")
    g.add_conditional_edges("agent", route, ["tools", END])
    g.add_edge("tools", "agent")
    return g.compile(checkpointer=InMemorySaver())


async def load_mcp_tools() -> tuple[list[BaseTool], dict[str, str]]:
    client = MultiServerMCPClient(settings.MCP_SERVERS)
    tools: list[BaseTool] = []
    servers: dict[str, str] = {}
    for name in settings.MCP_SERVERS:
        for tool in await client.get_tools(server_name=name):
            tools.append(tool)
            servers[tool.name] = name
    return tools, servers
