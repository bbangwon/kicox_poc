"""검색 · 그래프 순수 함수 단위 테스트 (DB · LLM 없이)."""

from backend.graph import _parse_json, _short_header
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
