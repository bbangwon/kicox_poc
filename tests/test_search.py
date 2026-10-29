"""검색 · 그래프 순수 함수 단위 테스트 (DB · LLM 없이)."""

from backend.graph import _parse_calls, _parse_json, _run_calls, _short_header, _tool_evidence
from backend.search import group_best, rrf


def test_rrf_sums_ranks_across_signals():
    scores, ranks = rrf({"dense": ["a", "b"], "bm25": ["b", "c"]}, k=60)
    assert scores["b"] == 1 / 62 + 1 / 61
    assert scores["a"] == 1 / 61
    assert ranks["b"] == {"dense": 2, "bm25": 1}
    assert max(scores, key=scores.get) == "b"


def test_group_best_keeps_top_point_per_key():
    best = group_best({"p1": 0.3, "p2": 0.5, "p3": 0.1}, {"p1": "d1", "p2": "d1", "p3": "d2"})
    assert best == {"d1": (0.5, "p2"), "d2": (0.1, "p3")}


def test_parse_json_tolerates_prose_and_fences():
    assert _parse_json('```json\n{"doc_ids": ["d1"], "reason": "x"}\n```') == {"doc_ids": ["d1"], "reason": "x"}
    assert _parse_json('답: {"a": {"b": 1}} 끝') == {"a": {"b": 1}}
    assert _parse_json("JSON 없음") == {}
    assert _parse_json('{"steps": ["잘린') == {}


def test_short_header_drops_document_name():
    assert _short_header("보수규정 시행세칙 > 제2장 > 제4조(휴직)") == "제2장 > 제4조(휴직)"
    assert _short_header("부칙") == "부칙"


def _tool(tool_id, name, inputs, steps, output="result"):
    spec = {"name": name, "kind": "formula", "description": name, "inputs": inputs, "steps": steps, "output": output}
    return {"tool_id": tool_id, "doc_id": "d", "doc_name": "복무규정", "section_title": "제4조", "page_start": 4, "page_end": 5, "spec": spec}


HOURS = _tool("t1", "compensatory_hours", [{"name": "hours", "type": "number", "unit": "시간"}], [{"name": "result", "expr": "hours * 1.5"}])
CAP = _tool(
    "t2",
    "annual_cap",
    [{"name": "used", "type": "number"}, {"name": "new", "type": "number"}],
    [{"name": "result", "expr": "min(new, 48 - used)"}],
)
TOOLS = {"t1": HOURS, "t2": CAP}


def test_run_calls_chains_previous_result():
    calls = [{"tool_id": "t1", "args": {"hours": "6"}}, {"tool_id": "t2", "args": {"used": 45, "new": "#1"}}]
    results, missing = _run_calls(calls, TOOLS)
    assert missing == []
    assert [r["value"] for r in results] == [9, 3]


def test_run_calls_reports_missing_and_skips_dependents():
    calls = [{"tool_id": "t1", "args": {"hours": None}}, {"tool_id": "t2", "args": {"used": 45, "new": "#1"}}]
    results, missing = _run_calls(calls, TOOLS)
    assert results == []
    assert missing == [{"call": 1, "input": "hours"}]  # #1을 받는 호출은 빈 입력으로 세지 않는다


def test_run_calls_keeps_tool_errors():
    results, missing = _run_calls([{"tool_id": "t1", "args": {"hours": "여섯"}}], TOOLS)
    assert missing == [] and "숫자가 아님" in results[0]["error"]


def test_tool_evidence_shows_inputs_steps_and_result():
    (r,), _ = _run_calls([{"tool_id": "t1", "args": {"hours": 6}}], TOOLS)
    e = _tool_evidence(r, HOURS, 4)
    assert e["n"] == 4 and e["kind"] == "tool" and e["verified"]
    assert "입력: hours (시간) = 6" in e["body"]
    assert "result = hours * 1.5 = 9" in e["body"] and "결과(result): 9" in e["body"]


def test_parse_calls_salvages_broken_json():
    text = '{"calls": [{"tool": "t2", "args": {"h": 6}}, "reason": "x"}, {"tool": "t1", "args": {"n": "#1"}}, "reason": "y"}]'
    assert _parse_json(text) == {}
    assert _parse_calls(text) == [{"tool": "t2", "args": {"h": 6}}, {"tool": "t1", "args": {"n": "#1"}}]
