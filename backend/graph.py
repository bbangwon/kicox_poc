"""LangGraph 오케스트레이션: planner → agent ⇄ tools.

각 노드는 `get_stream_writer()`로 시각화용 커스텀 이벤트를 내보낸다.
이벤트 스키마는 `type` 필드로 구분한다 (README 참고).
"""

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from functools import wraps
from typing import Annotated, Literal

import httpx
import ollama
from langchain_core.messages import AIMessage, AnyMessage, SystemMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_ollama import ChatOllama
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from typing_extensions import TypedDict

from backend import config as settings


class AgentState(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]
    plan: list[str]


def _llm(thinking: bool) -> ChatOllama:
    return ChatOllama(
        base_url=settings.OLLAMA_BASE_URL,
        model=settings.MODEL,
        reasoning=thinking,
        num_ctx=settings.NUM_CTX,
        temperature=settings.TEMPERATURE,
        keep_alive="30m",
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
    meta = msg.response_metadata or {}
    eval_count = meta.get("eval_count")
    eval_ns = meta.get("eval_duration")
    return {
        "ms": ms,
        "input_tokens": meta.get("prompt_eval_count"),
        "output_tokens": eval_count,
        "tps": round(eval_count / (eval_ns / 1e9), 1) if eval_count and eval_ns else None,
    }


def _retryable(e: Exception) -> bool:
    """서버 측 일시 오류만 재시도한다. 4xx(모델 없음 등)는 재시도해도 소용없으므로 제외."""
    if isinstance(e, ollama.ResponseError):
        # status_code -1: 스트리밍 도중 서버가 보낸 오류 (CUDA 오류 등)
        return e.status_code >= 500 or e.status_code == -1
    return isinstance(e, (ConnectionError, httpx.TransportError))


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


PLANNER_PROMPT = """당신은 업무 자동화 에이전트의 플래너입니다.
사용자의 마지막 요청을 처리하기 위한 실행 계획을 짧은 한국어 단계로 작성하세요.
마지막 메시지가 직전 역질문에 대한 답변이면, 이전 대화의 원래 요청과 합쳐서 계획을 세웁니다.
사용 가능한 도구:
{tools}

규칙:
- 도구가 필요 없으면 단계 하나("직접 답변")만 작성합니다.
- 판단에 필요한 정보가 부족하면 "사용자에게 부족한 정보 역질문" 단계를 포함합니다.
- 각 단계는 한 문장, 최대 5단계.
- 반드시 JSON으로만 응답: {{"steps": ["...", "..."]}}"""

AGENT_PROMPT = """당신은 사내 업무를 돕는 한국어 AI 에이전트입니다.
필요하면 제공된 도구를 호출하고, 도구 결과에 근거해서만 수치를 말하세요.
수치 계산은 반드시 calculate 도구를 사용하세요.
모든 작업이 끝나면 수행한 내용을 간결하게 한국어로 보고하세요.

개인교육훈련(교육비 지원 여부, 과세 여부, 이수학점, 교육신청) 질문:
- 답변 전에 반드시 get_training_policy로 규정을 확인하고, 규정 문구를 근거로 판단하세요.
- 이수학점은 calculate_training_credits 도구로 계산하세요. 사용자가 말하지 않은 인자는 추측하지 말고 생략하세요.

역질문(멀티턴) 규칙:
- 판단에 꼭 필요한 정보가 대화에 없으면 추측하거나 가정해서 답하지 말고, 사용자에게 되물은 뒤 답변을 마치세요.
  예) 학점 질문인데 사내외교육·사회공헌활동·학위과정 중 무엇인지 모름, 교육 시간을 모름,
      지원 여부 질문인데 교육 내용이 현직무·IDP 희망직무와 관련 있는지 모름.
- 도구가 status="need_info"를 반환하면 questions의 내용을 사용자에게 질문하세요.
- 질문은 한 번에 모아서 짧은 번호 목록으로 하고, 가능하면 선택지를 함께 제시하세요.
- 사용자가 답하면 이전 대화의 원래 질문과 합쳐서 다시 판단하세요.
- 결과에 conditions가 있으면 "~인 경우에 한해 인정"처럼 조건을 함께 안내하세요.

실행 계획:
{plan}"""


def build_graph(tools: list[BaseTool], tool_servers: dict[str, str]):
    tools_by_name = {t.name: t for t in tools}
    tool_catalog = "\n".join(f"- {t.name}: {t.description.splitlines()[0]}" for t in tools)

    @_traced("planner")
    async def planner(state: AgentState, config: RunnableConfig):
        llm = _llm(thinking=False).bind(format="json")
        started = time.perf_counter()
        msg = await _with_retry(
            "planner",
            lambda: llm.ainvoke([SystemMessage(PLANNER_PROMPT.format(tools=tool_catalog)), *state["messages"]]),
        )
        try:
            steps = [str(s) for s in json.loads(_text(msg.content)).get("steps", [])]
        except (json.JSONDecodeError, AttributeError):
            steps = []
        steps = steps or ["직접 답변"]
        _emit("plan", steps=steps, stats=_llm_stats(msg, round((time.perf_counter() - started) * 1000)))
        return {"plan": steps}

    @_traced("agent")
    async def agent(state: AgentState, config: RunnableConfig):
        thinking = config["configurable"].get("thinking", True)
        llm = _llm(thinking).bind_tools(tools)
        plan = "\n".join(f"{i}. {s}" for i, s in enumerate(state.get("plan", []), 1))
        messages = [SystemMessage(AGENT_PROMPT.format(plan=plan)), *state["messages"]]

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

        # additional_kwargs의 reasoning_content(Thinking)를 반드시 유지한다. ChatOllama는 이를 다음 호출 때
        # assistant 메시지의 `thinking` 필드로 되돌려 보내는데, 이전 턴의 Thinking이 빠지면 qwen3.6이
        # 도구 결과를 받은 뒤 Thinking만 하고 빈 응답으로 종료하는 현상이 자주 발생한다 (8회 중 4회 → 24회 중 0회).
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
