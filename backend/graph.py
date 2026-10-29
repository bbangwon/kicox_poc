"""LangGraph RAG 질의 처리: analyze → route → plan → read → (tool) → answer (설계 노트 §4, RAG_FLOW_OVERVIEW §4).

- analyze: 범위 밖 판정 · 검색용 질의 재작성 (짧은 LLM 호출, thinking off)
- route: 문서 카탈로그 하이브리드 검색 → router LLM이 문서 최대 3개 선택
- plan: 선택 문서의 parent 요약 · 청크 적중 표시를 보고 planner LLM이 읽을 child 선택.
  근거가 없으면 고른 문서를 빼고 route로 1회 되돌아간다 (§4.1 3-1). 선택 문서의 승인 도구도 여기서 불러온다
- read: child 원문 + 앞뒤 이웃 조회, 근거 번호 부여
- tool: 선택 문서에 승인 도구가 있을 때만. LLM이 도구 · 입력값을 고르고(값을 추측하지 않음) 실행기가 계산한다.
  결과는 근거 목록 끝에 번호를 붙여 넣는다
- ask: 입력값이 비면 되묻고 턴을 끝낸다 (최대 2회, 그 뒤에는 조건부로 답변). 대기 중인 호출은 state에 남는다
- resume: 대기 중인 호출이 있을 때 다음 턴의 시작. 새 메시지가 역질문의 답이면 값을 채워 다시 라우팅하지 않고
  계산하고, 새 질문이면 대기 호출을 버리고 analyze로 간다 (설계 노트 §7 미정 항목: state의 대기 슬롯 + LLM 판정)
- answer: 근거만으로 스트리밍 답변, 문장마다 [n] 인용
- no_reference: 참조문서 없음 안내

각 노드는 `get_stream_writer()`로 화면용 커스텀 이벤트를 내보낸다 (README 참고).
"""

import asyncio
import json
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict
from functools import wraps
from typing import Annotated, Literal

import httpx
import openai
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from typing_extensions import TypedDict

from backend import config as settings
from backend import search, tools_exec
from backend.llm import ChatVLLM

MAX_DOCS = 3
MAX_CHUNKS = 6
MAX_REROUTES = 1
MAX_CALLS = 3  # 한 질문에서 이어 부르는 도구 호출 수 (예: 보상휴가 시간 → 연간 한도)
MAX_ASKS = 2  # 역질문 상한 (설계 노트 §5.1)
HISTORY_MESSAGES = 6  # analyze · answer에 넘기는 이전 대화 (사용자 · 답변 합계)


class AgentState(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]
    queries: list[str]
    selected_docs: list[str]
    excluded_docs: list[str]
    reroutes: int
    chunk_ids: list[str]
    evidence: list[dict]
    no_reference: str | None  # 참조문서 없음 사유
    out_of_scope: bool
    tools: list[dict]  # 선택 문서의 승인 도구 (search.Tool)
    # 도구 호출 [{tool_id, args: {입력: 값 | "#n" | None}}]. "#n"은 n번째 호출 결과
    tool_calls: list[dict]
    tool_missing: list[dict]  # 값이 빈 입력 [{call: 1부터, input}]
    tool_note: str | None  # 답변 프롬프트에 붙일 도구 실패 · 입력 부족 안내
    asks: int  # 지금 호출에 대해 되물은 횟수
    pending: bool  # 역질문 답을 기다리는 중 (다음 턴은 resume에서 시작)


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


def _parse_json(text: str) -> dict:
    """응답에서 첫 JSON 객체를 꺼낸다. 앞뒤 설명 · 코드 펜스가 붙어도 되고, 실패하면 빈 dict."""
    start = text.find("{")
    if start < 0:
        return {}
    for end in range(len(text), start, -1):
        if text[end - 1] != "}":
            continue
        try:
            value = json.loads(text[start:end])
            return value if isinstance(value, dict) else {}
        except json.JSONDecodeError:
            continue
    return {}


def _parse_calls(text: str) -> list[dict]:
    """도구 호출 응답에서 {"tool": ...} 객체를 하나씩 꺼낸다. 전체 JSON이 깨져도 살린다
    (예: "reason"을 calls 목록 안에 끼워 넣는 응답)."""
    decoder = json.JSONDecoder()
    calls = []
    for m in re.finditer(r'\{\s*"tool"', text):
        try:
            value, _ = decoder.raw_decode(text, m.start())
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            calls.append(value)
    return calls


async def _ask(node: str, system: str, messages: list[AnyMessage]) -> tuple[str, dict]:
    """thinking off 짧은 호출. JSON 모드(response_format)는 쓰지 않는다 (seed32-think가 가끔 끝나지 않음)."""
    llm = _llm(thinking=False).bind(max_tokens=settings.PLANNER_MAX_TOKENS)
    started = time.perf_counter()
    msg = await _with_retry(node, lambda: llm.ainvoke([SystemMessage(system), *messages]))
    return _text(msg.content), _llm_stats(msg, round((time.perf_counter() - started) * 1000))


async def _ask_json(node: str, system: str, messages: list[AnyMessage]) -> tuple[dict, dict]:
    text, stats = await _ask(node, system, messages)
    return _parse_json(text), stats


def _history(state: AgentState) -> list[AnyMessage]:
    return [m for m in state["messages"] if isinstance(m, (HumanMessage, AIMessage))][-HISTORY_MESSAGES:]


def _short_header(header: str) -> str:
    """'문서 > 장 > 조' 머리말에서 문서명을 뺀 뒤쪽."""
    parts = [p.strip() for p in header.split(">")]
    return " > ".join(parts[1:]) if len(parts) > 1 else header


def _evidence_blocks(evidence: list[dict]) -> str:
    return "\n\n".join(f"[{e['n']}] {e['doc_name']} > {e['header']} (p.{e['page_start']})\n{e['body']}" for e in evidence)


# ── 계산 도구 ────────────────────────────────────────────────────────


def _num(v) -> str:
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return f"{v:,}" if isinstance(v, (int, float)) and not isinstance(v, bool) else str(v)


def _input_label(inp: dict) -> str:
    label = inp.get("description") or inp["name"]
    if inp.get("enum"):
        return f"{label} ({' / '.join(inp['enum'])} 중)"
    return f"{label} ({inp['unit']})" if inp.get("unit") else label


def _input_of(tool: dict, name: str) -> dict:
    return next(i for i in tool["spec"]["inputs"] if i["name"] == name)


def _tool_listing(tools: list[dict]) -> tuple[str, dict[str, dict]]:
    """도구를 t1, t2… 별칭으로 보여 준다 (이름을 베끼다 틀리는 일을 막는다)."""
    alias = {f"t{i}": t for i, t in enumerate(tools, 1)}
    lines = []
    for a, t in alias.items():
        spec = t["spec"]
        lines.append(f"{a}. {spec['name']}: {spec.get('description', '')} ({t['doc_name']} p.{t['page_start']})")
        for inp in spec["inputs"]:
            lines.append(f"   - {inp['name']}: {_input_label(inp)} [{inp['type']}]")
    return "\n".join(lines), alias


def _run_calls(calls: list[dict], tools: dict[str, dict]) -> tuple[list[dict], list[dict]]:
    """호출을 앞에서부터 실행한다. → (결과 [{call, tool_id, args, value, steps | error}], 빈 입력 [{call, input}]).

    값이 빈 호출과, 실행하지 못한 호출의 결과("#n")를 받는 호출은 건너뛴다.
    """
    results, missing = [], []
    values: dict[int, object] = {}
    for i, call in enumerate(calls, 1):
        tool = tools[call["tool_id"]]
        args, ready = {}, True
        for inp in tool["spec"]["inputs"]:
            v = call["args"].get(inp["name"])
            if isinstance(v, str) and (m := re.fullmatch(r"#(\d+)", v.strip())):
                v = values.get(int(m[1]))
                ready = ready and v is not None
            elif v is None or v == "":
                missing.append({"call": i, "input": inp["name"]})
                ready = False
            args[inp["name"]] = v
        if not ready:
            continue
        tables = {
            t["spec"]["name"]: t["spec"]
            for t in tools.values()
            if t["doc_id"] == tool["doc_id"] and t["spec"]["kind"] == "table_lookup"
        }
        try:
            out = tools_exec.run(tool["spec"], args, tables)
        except Exception as e:  # ToolError(입력 형식 · 표에 없는 값), 0으로 나누기 등
            results.append({"call": i, "tool_id": tool["tool_id"], "args": args, "error": str(e)})
            continue
        values[i] = out["value"]
        results.append({"call": i, "tool_id": tool["tool_id"], "args": args, **out})
    return results, missing


def _tool_evidence(r: dict, tool: dict, n: int) -> dict:
    """도구 결과를 원문 근거와 같은 모양으로 만든다 (답변의 [n] 인용 · 화면 근거 목록)."""
    spec = tool["spec"]
    exprs = {s["name"]: s["expr"] for s in spec.get("steps", [])}
    lines = [
        f"계산 도구 {spec['name']}: {spec.get('description', '')}",
        "입력: " + ", ".join(f"{_input_label(_input_of(tool, k))} = {_num(v)}" for k, v in r["args"].items()),
    ]
    if r["steps"]:
        lines.append("계산: " + " → ".join(f"{k} = {exprs.get(k, '')} = {_num(v)}" for k, v in r["steps"].items()))
    unit = f" {spec['output_unit']}" if spec.get("output_unit") else ""
    lines.append(f"결과({spec['output']}): {_num(r['value'])}{unit}")
    if spec.get("source_quote"):
        lines.append(f"근거 문구: “{spec['source_quote']}”")
    return {
        "n": n,
        "kind": "tool",
        "chunk_id": f"tool:{tool['tool_id']}:{r['call']}",
        "doc_name": tool["doc_name"],
        "section_title": tool["section_title"],
        "header": f"계산 도구 · {spec['name']}",
        "body": "\n".join(lines),
        "page_start": tool["page_start"],
        "page_end": tool["page_end"],
        "has_numbers": True,
        "verified": True,  # 사람이 원문과 대조해 승인한 도구만 적재된다
        "neighbor": False,
    }


def _missing_labels(missing: list[dict], calls: list[dict], tools: dict[str, dict]) -> list[str]:
    return [_input_label(_input_of(tools[calls[m["call"] - 1]["tool_id"]], m["input"])) for m in missing]


def _execute(state: AgentState, calls: list[dict], asks: int) -> dict:
    """도구 호출을 실행해 근거에 붙이고, 값이 비면 역질문 대기(pending)로 둔다."""
    tools = {t["tool_id"]: t for t in state["tools"]}
    results, missing = _run_calls(calls, tools)
    evidence = [e for e in state["evidence"] if e.get("kind") != "tool"]  # 이어받은 턴의 이전 계산 결과는 버린다
    n = max((e["n"] for e in evidence), default=0)
    ok = [r for r in results if "error" not in r]
    added = [_tool_evidence(r, tools[r["tool_id"]], n + i) for i, r in enumerate(ok, 1)]
    labels = _missing_labels(missing, calls, tools)
    pending = bool(missing) and asks < MAX_ASKS
    _emit(
        "tool_results",
        items=[{**r, "name": tools[r["tool_id"]]["spec"]["name"]} for r in results],
        missing=labels,
        pending=pending,
    )
    if added:
        _emit("evidence", items=evidence + added)
    notes = [
        f"- 계산 도구 {tools[r['tool_id']]['spec']['name']} 실행 실패({r['error']}). 이 계산 결과는 말하지 말고 근거 원문으로만 답하세요."
        for r in results
        if "error" in r
    ]
    if missing and not pending:
        notes.append(
            f"- 계산에 필요한 값을 받지 못했습니다: {', '.join(labels)}. 값을 추측해 계산하지 말고, "
            "근거의 기준을 경우별로 나눠(\"~라면 ○○\") 조건부로 답하세요."
        )
    return {
        "tool_calls": calls,
        "tool_missing": missing,
        "tool_note": "\n".join(notes) or None,
        "evidence": evidence + added,
        "asks": asks,
        "pending": pending,
    }


# ── 프롬프트 ─────────────────────────────────────────────────────────

ANALYZE_PROMPT = """당신은 한국산업단지공단 내부규정 Q&A의 질의 분석기입니다.
대화의 마지막 사용자 메시지를 보고, 규정 검색이 필요한지 판단하고 검색용 질의를 만드세요.

규칙:
- out_of_scope=true: 인사말·잡담, 규정과 무관한 요청, 개인별 기록 조회(내 포인트 잔액, 내 이번 달 급여액처럼
  회사 시스템의 개인 데이터를 봐야만 답할 수 있는 것).
- 조건을 붙여 기준을 묻는 질문은 "제가", "저는"으로 시작해도 규정 질문입니다(out_of_scope=false).
  예: "변호사 자격증 있으면 연봉 더 받아요?", "제가 육아휴직하면 보수 나와요?" → 규정의 지급 기준을 묻는 것.
- 규정 내용(기준·절차·금액 기준표 등)을 묻는 질문은 어떤 규정인지 몰라도 out_of_scope=false입니다.
- queries: 검색용 질의 1~3개. 이전 대화 맥락을 반영해 완결된 질의로 다시 씁니다("그럼 1년 넘으면요?" → "질병 휴직 1년 초과 보수").
  구어는 규정 용어로 바꿉니다(월급 → 보수, 기름값 → 자동차 운임). 질문이 여러 개면 나눕니다.
- 반드시 JSON으로만 응답: {"out_of_scope": false, "reason": "한 문장", "queries": ["..."]}"""

ROUTE_PROMPT = """당신은 내부규정 Q&A의 라우터입니다. 검색 후보 문서 중에서 사용자 질문에 답할 근거가 있을 문서를 고르세요.

질문(검색용으로 다시 쓴 것): {queries}

후보 문서:
{candidates}

규칙:
- 질문에 직접 답할 수 있는 문서만 최대 {max_docs}개 고릅니다. 두 문서에 걸친 질문이면 둘 다 고릅니다.
- "다루지 않음"에 해당하면 고르지 않습니다. 이름이 비슷하다는 이유만으로 고르지 않습니다.
- 맞는 문서가 없으면 빈 목록을 냅니다. 억지로 고르지 마세요.
- 반드시 JSON으로만 응답: {{"doc_ids": ["d1"], "reason": "한 문장"}}"""

PLAN_PROMPT = """당신은 내부규정 Q&A의 planner입니다. 선택된 문서의 목차에서 질문에 답하는 데 읽어야 할 청크를 고르세요.

질문(검색용으로 다시 쓴 것): {queries}

목차 (★ = 검색 적중, 절 아래 c번호가 청크):
{sections}

규칙:
- 답의 근거가 들어 있을 청크를 최대 {max_chunks}개 고릅니다. ★가 붙지 않은 청크도 고를 수 있습니다.
- 각 절의 "다루지 않음"에 해당하는 내용이면 그 절에서 고르지 않습니다.
- 목차 어디에도 질문에 답할 내용이 없으면 빈 목록을 냅니다.
- 반드시 JSON으로만 응답: {{"chunks": ["c1", "c2"], "reason": "한 문장"}}"""

TOOL_PROMPT = """당신은 내부규정 Q&A의 계산 도구 호출기입니다. 질문에 답하는 데 아래 계산 도구가 필요한지 판단하고, 필요하면 호출할 도구와 입력값을 정하세요.

질문(검색용으로 다시 쓴 것): {queries}

계산 도구:
{tools}

근거 원문:
{evidence}

규칙:
- 질문이 도구가 계산하는 수치(금액·일수·시간·학점 등)를 물을 때만 호출합니다. 기준·절차·대상 여부만 묻는 질문이면 빈 목록을 냅니다.
- 계산 방법·기준만 묻는 질문("어떻게 계산해요?")도 빈 목록을 냅니다. 자기 경우의 값을 묻는 질문("저는 며칠이에요?")이면 호출합니다.
- 입력값은 사용자가 대화에서 말한 값, 또는 사용자가 말한 조건으로 근거 원문에서 바로 정해지는 값(예: 직급별 상한표의 금액)만 씁니다.
  알 수 없는 값은 추측하지 말고 null로 둡니다. 빈 값은 사용자에게 되묻습니다.
- 단위는 도구 입력에 맞춰 바꿉니다("3시간 30분" → 3.5, "40만 원" → 400000). 선택지가 있는 입력은 목록의 값을 그대로 씁니다.
- 앞 호출의 결과를 다음 호출의 입력으로 쓰려면 "#1"처럼 호출 순번을 씁니다. 호출은 최대 {max_calls}개입니다.
- 반드시 JSON으로만 응답: {{"reason": "한 문장", "calls": [{{"tool": "t1", "args": {{"입력이름": 값}}}}]}}"""

RESUME_PROMPT = """당신은 내부규정 Q&A에서 계산에 필요한 값을 사용자에게 되물은 상태입니다.
대화의 마지막 사용자 메시지가 이 물음에 대한 답인지 판단하세요.

되물은 항목:
{missing}

규칙:
- 답이 아니라 새 질문이거나 주제가 바뀌었으면 answer=false입니다.
- 값을 모른다는 답("잘 모르겠어요")도 answer=true이고, 그 항목은 null입니다.
- 답이면 메시지에 있는 값만 채우고, 없는 항목은 null로 둡니다. 추측하지 마세요.
- 단위는 각 항목 단위로 바꿉니다("2년 반" → 2.5). 선택지가 있는 항목은 목록의 값을 그대로 씁니다.
- 반드시 JSON으로만 응답: {{"answer": true, "values": {{"m1": 값}}, "reason": "한 문장"}}"""

ANSWER_PROMPT = """당신은 한국산업단지공단 직원의 내부규정 질문에 답하는 한국어 상담 에이전트입니다.

## 근거
{evidence}

## 원칙
- 위 근거에 있는 내용만 말하세요. 기억이나 일반 상식으로 규정을 보충하지 마세요.
- 수치(금액·비율·일수·포인트)는 근거에 적힌 그대로 옮기세요. 근거에 없는 수치를 계산하거나 추정하지 마세요.
- "계산 도구" 근거는 승인된 계산 도구가 규정의 식으로 계산한 결과입니다. 결과와 사용한 입력값을 그대로 쓰고 인용하세요.
  직접 다시 계산하지 마세요.
- 근거로 답할 수 없는 부분은 "규정에서 확인되지 않습니다"라고 말하세요. 개인별 데이터는 조회할 수 없습니다.
{tool_note}
## 답변 형식
- 첫 문장에 결론을 쓰세요.
- 이어서 조건·예외를 2~5개의 짧은 글머리표로 쓰세요.
- 근거를 쓴 문장 끝에 [1], [2]처럼 근거 번호를 붙이세요. 여러 개면 [1][3]. 근거 목록에 없는 번호는 쓰지 마세요.
- 처리 과정 설명, 태그·HTML, "추가 질문이 있으면…" 같은 맺음말은 쓰지 마세요."""


def _no_reference_text(out_of_scope: bool, reason: str, docs: list[dict]) -> str:
    names = "\n".join(f"- {d['name']}" for d in docs) or "- (없음)"
    head = (
        "내부규정 내용에 대한 질문에 답변드리고 있습니다. 개인별 데이터는 조회할 수 없습니다."
        if out_of_scope
        else f"관련 규정을 찾지 못해 답변드리기 어렵습니다. ({reason})"
    )
    return (
        f"{head}\n\n지금 답변할 수 있는 규정은 다음과 같습니다.\n{names}\n\n"
        "개인별 데이터나 위 목록에 없는 규정은 담당 부서에 문의해 주세요."
    )


def build_graph():
    @_traced("analyze")
    async def analyze(state: AgentState, config: RunnableConfig):
        parsed, stats = await _ask_json("analyze", ANALYZE_PROMPT, _history(state))
        queries = [str(q).strip() for q in parsed.get("queries") or [] if str(q).strip()][:3]
        last = _text(state["messages"][-1].content)
        out = bool(parsed.get("out_of_scope"))
        if not out and not queries:
            queries = [last]  # 파싱 실패 시 원문으로 검색한다
        reason = str(parsed.get("reason") or "")
        _emit("analysis", out_of_scope=out, reason=reason, queries=queries, stats=stats)
        # 턴마다 라우팅 상태를 새로 시작한다
        return {
            "queries": queries,
            "selected_docs": [],
            "excluded_docs": [],
            "reroutes": 0,
            "chunk_ids": [],
            "evidence": [],
            "no_reference": (reason or "규정 질문이 아닙니다") if out else None,
            "out_of_scope": out,
            "tools": [],
            "tool_calls": [],
            "tool_missing": [],
            "tool_note": None,
            "asks": 0,
            "pending": False,
        }

    @_traced("route")
    async def route(state: AgentState, config: RunnableConfig):
        candidates, ms = await search.search_docs(state["queries"])
        excluded = set(state["excluded_docs"])
        shown = [c for c in candidates if c.doc_id not in excluded]
        _emit(
            "doc_candidates",
            ms=ms,
            dense=search.dense_enabled(),
            attempt=state["reroutes"] + 1,
            items=[{**asdict(c), "excluded": c.doc_id in excluded} for c in candidates],
        )
        if not shown:
            return {"selected_docs": [], "no_reference": "검색 후보 문서가 없습니다"}

        # 후보는 d1, d2… 짧은 별칭으로 보여 준다 (doc_id를 그대로 베끼다 틀리는 일을 막는다)
        alias = {f"d{i}": c for i, c in enumerate(shown, 1)}
        listing = "\n".join(
            f"{a}. {c.name}\n   요약: {c.summary}\n   다루지 않음: {c.not_for or '-'}" for a, c in alias.items()
        )
        parsed, stats = await _ask_json(
            "route",
            ROUTE_PROMPT.format(queries=" / ".join(state["queries"]), candidates=listing, max_docs=MAX_DOCS),
            _history(state)[-1:],
        )
        picked = [alias[a].doc_id for a in parsed.get("doc_ids") or [] if a in alias][:MAX_DOCS]
        _emit(
            "route",
            doc_ids=picked,
            names=[c.name for c in shown if c.doc_id in picked],
            reason=str(parsed.get("reason") or ""),
            stats=stats,
        )
        if not picked:
            return {"selected_docs": [], "no_reference": "질문에 맞는 규정 문서가 없습니다"}
        return {"selected_docs": picked}

    @_traced("plan")
    async def plan(state: AgentState, config: RunnableConfig):
        docs = state["selected_docs"]
        sections = await search.list_sections(docs)
        hits_list = []
        hit_ms = 0
        for q in state["queries"]:
            hits, ms = await search.search_chunks(q, docs)
            hits_list.append(hits)
            hit_ms += ms
        hit_sections = {h.section_id for hits in hits_list for h in hits if h.summary_hit or h.child_ids}
        hit_children = {c for hits in hits_list for h in hits for c in h.child_ids}
        by_id = {s.section_id: s for s in sections}
        _emit(
            "chunk_hits",
            ms=hit_ms,
            sections=[
                {
                    "section_id": h.section_id,
                    "doc_name": by_id[h.section_id].doc_name if h.section_id in by_id else "",
                    "title": by_id[h.section_id].title if h.section_id in by_id else h.section_id,
                    "score": h.score,
                    "summary_hit": h.summary_hit,
                    "child_hits": len(h.child_ids),
                }
                for h in hits_list[0][:10]
            ],
        )

        # 청크 머리말 · 본문 앞부분을 목차에 붙여 보여 준다
        chunks = {c.chunk_id: c for c in await search.get_chunks([c for s in sections for c in s.chunk_ids], 0)}
        alias: dict[str, str] = {}
        lines = []
        doc_name = None
        for s in sections:
            if s.doc_name != doc_name:
                doc_name = s.doc_name
                lines.append(f"# {doc_name}")
            mark = " ★" if s.section_id in hit_sections else ""
            lines.append(f"## {s.title} (p.{s.page_start}-{s.page_end}){mark}")
            lines.append(f"   다룸: {s.covers or '-'}")
            if s.not_covers:
                lines.append(f"   다루지 않음: {s.not_covers}")
            for cid in s.chunk_ids:
                c = chunks.get(cid)
                if not c:
                    continue
                a = f"c{len(alias) + 1}"
                alias[a] = cid
                preview = re.sub(r"\s+", " ", c.body)[:50]
                lines.append(f"   {a}{' ★' if cid in hit_children else ''} {_short_header(c.header)[-40:]} | {preview}")
        parsed, stats = await _ask_json(
            "plan",
            PLAN_PROMPT.format(queries=" / ".join(state["queries"]), sections="\n".join(lines), max_chunks=MAX_CHUNKS),
            _history(state)[-1:],
        )
        picked = [alias[a] for a in parsed.get("chunks") or [] if a in alias][:MAX_CHUNKS]
        _emit(
            "plan",
            chunk_ids=picked,
            labels=[_short_header(chunks[c].header) for c in picked],
            reason=str(parsed.get("reason") or ""),
            stats=stats,
        )
        if picked:
            tools = [asdict(t) for t in await search.list_tools(docs)]
            if tools:
                _emit(
                    "tools",
                    items=[
                        {"name": t["spec"]["name"], "description": t["spec"].get("description", ""), "doc_name": t["doc_name"]}
                        for t in tools
                    ],
                )
            return {"chunk_ids": picked, "tools": tools}
        if state["reroutes"] < MAX_REROUTES:
            return {"chunk_ids": [], "excluded_docs": [*state["excluded_docs"], *docs], "reroutes": state["reroutes"] + 1}
        return {"chunk_ids": [], "no_reference": "선택한 문서에서 근거를 찾지 못했습니다"}

    @_traced("read")
    async def read(state: AgentState, config: RunnableConfig):
        chunks = await search.get_chunks(state["chunk_ids"], neighbors=1)
        evidence = [
            {
                "n": i,
                "chunk_id": c.chunk_id,
                "doc_name": c.doc_name,
                "section_title": c.section_title,
                "header": _short_header(c.header),
                "body": c.body,
                "page_start": c.page_start,
                "page_end": c.page_end,
                "has_numbers": c.has_numbers,
                "verified": c.verified,
                "neighbor": c.neighbor,
            }
            for i, c in enumerate(chunks, 1)
        ]
        _emit("evidence", items=evidence)
        return {"evidence": evidence}

    @_traced("answer")
    async def answer(state: AgentState, config: RunnableConfig):
        thinking = config["configurable"].get("thinking", True)
        llm = _llm(thinking)
        note = state.get("tool_note")
        system = ANSWER_PROMPT.format(evidence=_evidence_blocks(state["evidence"]), tool_note=f"{note}\n" if note else "")
        messages = [SystemMessage(system), *_history(state)]

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
        full = await _with_retry("answer", stream_once)
        # reasoning_content(Thinking)는 다음 호출에 보내지 않는다 (backend/llm.py 참고)
        ai = AIMessage(content=full.content, usage_metadata=full.usage_metadata, id=full.id)
        _emit("llm_end", stats=_llm_stats(full, round((time.perf_counter() - started) * 1000)))
        return {"messages": [ai]}

    @_traced("tool")
    async def tool(state: AgentState, config: RunnableConfig):
        listing, alias = _tool_listing(state["tools"])
        text, stats = await _ask(
            "tool",
            TOOL_PROMPT.format(
                queries=" / ".join(state["queries"]),
                tools=listing,
                evidence=_evidence_blocks(state["evidence"]),
                max_calls=MAX_CALLS,
            ),
            _history(state),  # 앞 턴에서 말한 값도 입력으로 쓴다
        )
        parsed = _parse_json(text)
        calls = []
        for c in parsed.get("calls") if isinstance(parsed.get("calls"), list) else _parse_calls(text):
            t = alias.get(str(c.get("tool"))) if isinstance(c, dict) else None
            if not t:
                continue
            args = c.get("args") if isinstance(c.get("args"), dict) else {}
            calls.append({"tool_id": t["tool_id"], "args": {i["name"]: args.get(i["name"]) for i in t["spec"]["inputs"]}})
        calls = calls[:MAX_CALLS]
        tools = {t["tool_id"]: t for t in state["tools"]}
        _emit(
            "tool_calls",
            resumed=False,
            calls=[{"name": tools[c["tool_id"]]["spec"]["name"], "args": c["args"]} for c in calls],
            reason=str(parsed.get("reason") or ""),
            stats=stats,
        )
        return _execute(state, calls, asks=0)

    @_traced("ask")
    async def ask(state: AgentState, config: RunnableConfig):
        tools = {t["tool_id"]: t for t in state["tools"]}
        labels = _missing_labels(state["tool_missing"], state["tool_calls"], tools)
        # 입력 설명이 없는 도구도 있으므로 어떤 계산인지 함께 보여 준다
        purposes = [
            p
            for p in dict.fromkeys(
                tools[state["tool_calls"][m["call"] - 1]["tool_id"]]["spec"].get("description", "")
                for m in state["tool_missing"]
            )
            if p
        ]
        head = f"계산하려면 다음 정보가 필요합니다 ({' · '.join(purposes)})." if purposes else "계산하려면 다음 정보가 필요합니다."
        text = head + "\n" + "\n".join(f"{i}. {label}" for i, label in enumerate(labels, 1))
        _emit("ask", missing=labels, asks=state["asks"] + 1, max_asks=MAX_ASKS)
        _emit("token", delta=text)
        return {"messages": [AIMessage(text)], "asks": state["asks"] + 1}

    @_traced("resume")
    async def resume(state: AgentState, config: RunnableConfig):
        tools = {t["tool_id"]: t for t in state["tools"]}
        calls = [{**c, "args": dict(c["args"])} for c in state["tool_calls"]]
        alias = {f"m{i}": m for i, m in enumerate(state["tool_missing"], 1)}
        listing = "\n".join(
            f"{a}. {_input_label(inp)} [{inp['type']}]"
            for a, m in alias.items()
            for inp in [_input_of(tools[calls[m["call"] - 1]["tool_id"]], m["input"])]
        )
        # 역질문과 그 답만 본다
        parsed, stats = await _ask_json("resume", RESUME_PROMPT.format(missing=listing), _history(state)[-2:])
        is_answer = bool(parsed.get("answer"))
        values = parsed.get("values") if isinstance(parsed.get("values"), dict) else {}
        filled = {a: v for a, v in values.items() if a in alias and v not in (None, "")}
        _emit(
            "resume",
            answer=is_answer,
            values={_input_label(_input_of(tools[calls[alias[a]["call"] - 1]["tool_id"]], alias[a]["input"])): v for a, v in filled.items()},
            reason=str(parsed.get("reason") or ""),
            stats=stats,
        )
        if not is_answer:
            return {"pending": False, "tool_calls": [], "tool_missing": []}
        for a, v in filled.items():
            calls[alias[a]["call"] - 1]["args"][alias[a]["input"]] = v
        # 값을 하나도 주지 못했으면(모른다는 답) 같은 질문을 되풀이하지 않고 조건부로 답한다
        return _execute(state, calls, state["asks"] if filled else MAX_ASKS)

    @_traced("no_reference")
    async def no_reference(state: AgentState, config: RunnableConfig):
        reason = state.get("no_reference") or "근거 없음"
        out = state.get("out_of_scope", False)
        text = _no_reference_text(out, reason, await search.documents())
        _emit("no_reference", reason=reason, out_of_scope=out)
        _emit("token", delta=text)
        return {"messages": [AIMessage(text)]}

    def after_start(state: AgentState) -> Literal["resume", "analyze"]:
        return "resume" if state.get("pending") else "analyze"

    def after_resume(state: AgentState) -> Literal["analyze", "ask", "answer"]:
        if not state["tool_calls"]:
            return "analyze"  # 역질문의 답이 아니라 새 질문
        return "ask" if state["pending"] else "answer"

    def after_read(state: AgentState) -> Literal["tool", "answer"]:
        return "tool" if state.get("tools") else "answer"

    def after_tool(state: AgentState) -> Literal["ask", "answer"]:
        return "ask" if state["pending"] else "answer"

    def after_analyze(state: AgentState) -> Literal["route", "no_reference"]:
        return "no_reference" if state.get("no_reference") else "route"

    def after_route(state: AgentState) -> Literal["plan", "no_reference"]:
        return "plan" if state["selected_docs"] else "no_reference"

    def after_plan(state: AgentState) -> Literal["read", "route", "no_reference"]:
        if state["chunk_ids"]:
            return "read"
        return "no_reference" if state.get("no_reference") else "route"

    g = StateGraph(AgentState)
    for name, fn in [
        ("analyze", analyze),
        ("route", route),
        ("plan", plan),
        ("read", read),
        ("tool", tool),
        ("ask", ask),
        ("resume", resume),
        ("answer", answer),
        ("no_reference", no_reference),
    ]:
        g.add_node(name, fn)
    g.add_conditional_edges(START, after_start, ["resume", "analyze"])
    g.add_conditional_edges("resume", after_resume, ["analyze", "ask", "answer"])
    g.add_conditional_edges("analyze", after_analyze, ["route", "no_reference"])
    g.add_conditional_edges("route", after_route, ["plan", "no_reference"])
    g.add_conditional_edges("plan", after_plan, ["read", "route", "no_reference"])
    g.add_conditional_edges("read", after_read, ["tool", "answer"])
    g.add_conditional_edges("tool", after_tool, ["ask", "answer"])
    g.add_edge("ask", END)
    g.add_edge("answer", END)
    g.add_edge("no_reference", END)
    return g.compile(checkpointer=InMemorySaver())
