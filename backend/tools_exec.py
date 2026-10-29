"""도구 spec 실행기 (kicox_regulation_prep/preprocess/tools_exec.py 사본, 원본을 고치면 같이 맞춘다).

spec의 식은 eval하지 않는다. ast로 파싱해 허용한 노드(숫자·문자열 상수, 이름, 사칙연산, 비교,
상수 목록에 대한 in, 조건식, 허용 함수 호출)만 직접 평가한다. 거듭제곱·속성·첨자·람다 등은 파싱 단계에서 거부한다.

kind
- formula: inputs → steps[{name, expr}] 순서대로 평가 → output step 값
- table_lookup: inputs 중 keys 값으로 rows에서 value를 찾는다
"""

import ast
import math
import re
from decimal import ROUND_HALF_UP, Decimal

KINDS = ("formula", "table_lookup")
INPUT_TYPES = ("number", "enum")
NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")


class ToolError(Exception):
    pass


def _round(x, ndigits=0):
    """원 단위 반올림 (파이썬 round의 오사오입이 아니라 사사오입)."""
    q = Decimal(1).scaleb(-int(ndigits))
    r = Decimal(str(x)).quantize(q, rounding=ROUND_HALF_UP)
    return int(r) if ndigits == 0 else float(r)


def _truncate(x, unit=1):
    """unit 미만 절사 (예: truncate(x, 10) → 10원 미만 버림)."""
    return math.floor(x / unit) * unit


FUNCS = {"min": min, "max": max, "round": _round, "floor": math.floor, "ceil": math.ceil, "truncate": _truncate}

_BINOPS = {
    ast.Add: lambda a, b: a + b,
    ast.Sub: lambda a, b: a - b,
    ast.Mult: lambda a, b: a * b,
    ast.Div: lambda a, b: a / b,
    ast.FloorDiv: lambda a, b: a // b,
    ast.Mod: lambda a, b: a % b,
}
_CMPOPS = {
    ast.Eq: lambda a, b: a == b,
    ast.NotEq: lambda a, b: a != b,
    ast.Lt: lambda a, b: a < b,
    ast.LtE: lambda a, b: a <= b,
    ast.Gt: lambda a, b: a > b,
    ast.GtE: lambda a, b: a >= b,
    ast.In: lambda a, b: a in b,
    ast.NotIn: lambda a, b: a not in b,
}


def _is_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _is_arith(v) -> bool:
    """사칙연산 피연산자: 숫자와 비교 결과(True=1, False=0)."""
    return isinstance(v, (int, float))


def parse(expr: str) -> ast.expr:
    """식을 파싱하고 허용 범위를 확인한다. 위반이면 ToolError."""
    if not isinstance(expr, str) or not expr.strip():
        raise ToolError("빈 식")
    if len(expr) > 500:
        raise ToolError("식이 너무 깁니다 (500자 초과)")
    try:
        tree = ast.parse(expr, mode="eval").body
    except SyntaxError as e:
        raise ToolError(f"식 문법 오류: {e.msg}") from None
    _check(tree)
    return tree


def _check(node: ast.AST) -> None:
    match node:
        case ast.Constant(value=v) if _is_num(v) or isinstance(v, str):
            pass
        case ast.Name():
            pass
        case ast.BinOp(op=op, left=l, right=r) if type(op) in _BINOPS:
            _check(l), _check(r)
        case ast.UnaryOp(op=ast.USub() | ast.UAdd(), operand=o):
            _check(o)
        case ast.Compare(left=l, ops=ops, comparators=cs) if all(type(o) in _CMPOPS for o in ops):
            _check(l)
            for op, c in zip(ops, cs):
                if isinstance(op, (ast.In, ast.NotIn)):
                    if not (isinstance(c, (ast.List, ast.Tuple)) and all(isinstance(e, ast.Constant) for e in c.elts)):
                        raise ToolError(f"in 뒤에는 상수 목록만: {ast.unparse(node)}")
                else:
                    _check(c)
        case ast.BoolOp(values=vs):
            for v in vs:
                _check(v)
        case ast.IfExp(test=t, body=b, orelse=o):
            _check(t), _check(b), _check(o)
        case ast.Call(func=ast.Name(id="lookup"), args=[ast.Constant(value=str()), *rest], keywords=[]):
            for a in rest:
                _check(a)
        case ast.Call(func=ast.Name(id=f), args=args, keywords=[]) if f in FUNCS:
            for a in args:
                _check(a)
        case ast.Call():
            raise ToolError(f"허용하지 않는 함수 호출: {ast.unparse(node)}")
        case _:
            raise ToolError(f"허용하지 않는 식: {ast.unparse(node)}")


def names_in(tree: ast.expr) -> set[str]:
    """식이 참조하는 변수 이름 (함수 이름 제외)."""
    funcs = {id(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call)}
    return {n.id for n in ast.walk(tree) if isinstance(n, ast.Name) and id(n) not in funcs}


def lookups_in(tree: ast.expr) -> list[tuple[str, int]]:
    """lookup("표", 키...) 호출의 (표 이름, 키 개수)."""
    return [
        (n.args[0].value, len(n.args) - 1)
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "lookup"
    ]


def numbers_in(tree: ast.expr) -> list[float]:
    return [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and _is_num(n.value)]


def _eval(node: ast.expr, env: dict, tables: dict):
    match node:
        case ast.Constant(value=v):
            return v
        case ast.Name(id=name):
            if name not in env:
                raise ToolError(f"정의되지 않은 이름: {name}")
            return env[name]
        case ast.List(elts=es) | ast.Tuple(elts=es):
            return [e.value for e in es]
        case ast.BinOp(op=op, left=l, right=r):
            a, b = _eval(l, env, tables), _eval(r, env, tables)
            if not (_is_arith(a) and _is_arith(b)):
                raise ToolError(f"숫자가 아닌 값의 연산: {ast.unparse(node)}")
            try:
                return _BINOPS[type(op)](a, b)
            except ZeroDivisionError:
                raise ToolError(f"0으로 나눔: {ast.unparse(node)}") from None
        case ast.UnaryOp(op=op, operand=o):
            v = _eval(o, env, tables)
            if not _is_arith(v):
                raise ToolError(f"숫자가 아닌 값의 부호: {ast.unparse(node)}")
            return -v if isinstance(op, ast.USub) else v
        case ast.Compare(left=l, ops=ops, comparators=cs):
            a = _eval(l, env, tables)
            for op, c in zip(ops, cs):
                b = _eval(c, env, tables)
                if not _CMPOPS[type(op)](a, b):
                    return False
                a = b
            return True
        case ast.BoolOp(op=op, values=vs):
            if isinstance(op, ast.And):
                return all(_eval(v, env, tables) for v in vs)
            return any(_eval(v, env, tables) for v in vs)
        case ast.IfExp(test=t, body=b, orelse=o):
            return _eval(b, env, tables) if _eval(t, env, tables) else _eval(o, env, tables)
        case ast.Call(func=ast.Name(id="lookup"), args=[ast.Constant(value=table), *rest]):
            if table not in tables:
                raise ToolError(f"없는 표: {table}")
            t = tables[table]
            return lookup(t, dict(zip(t["keys"], (_eval(a, env, tables) for a in rest))))
        case ast.Call(func=ast.Name(id=f), args=args):
            vals = [_eval(a, env, tables) for a in args]
            if not all(_is_arith(v) for v in vals):
                raise ToolError(f"숫자가 아닌 인자: {ast.unparse(node)}")
            return FUNCS[f](*vals)
    raise ToolError(f"평가할 수 없는 식: {ast.unparse(node)}")


def lookup(spec: dict, key: dict):
    for row in spec["rows"]:
        if all(str(row["key"].get(k)) == str(key.get(k)) for k in spec["keys"]):
            return row["value"]
    raise ToolError(f"{spec['name']} 표에 없는 값: {key}")


def coerce_args(spec: dict, args: dict) -> dict:
    """입력을 spec의 inputs 정의에 맞춘다 (숫자 문자열의 쉼표 허용, enum 값 확인)."""
    out = {}
    for inp in spec["inputs"]:
        name = inp["name"]
        if name not in args or args[name] in (None, ""):
            raise ToolError(f"입력 없음: {name}")
        v = args[name]
        if inp["type"] == "number":
            try:
                v = float(str(v).replace(",", "")) if not _is_num(v) else v
            except ValueError:
                raise ToolError(f"{name}: 숫자가 아님 ({v})") from None
            if isinstance(v, float) and v.is_integer():
                v = int(v)
        else:
            v = str(v)
            if inp.get("enum") and v not in inp["enum"]:
                raise ToolError(f"{name}: {inp['enum']} 중 하나여야 함 ({v})")
        out[name] = v
    return out


def run(spec: dict, args: dict, tables: dict) -> dict:
    """spec을 실행해 {"value", "steps"}를 돌려준다. tables는 같은 문서의 table_lookup spec (name → spec)."""
    env = coerce_args(spec, args)
    if spec["kind"] == "table_lookup":
        return {"value": lookup(spec, env), "steps": {}}
    steps = {}
    for step in spec["steps"]:
        steps[step["name"]] = env[step["name"]] = _eval(parse(step["expr"]), env, tables)
    return {"value": steps[spec["output"]], "steps": steps}


def validate(spec: dict, tables: dict) -> list[str]:
    """spec의 형식 오류 목록. 비어 있으면 실행할 수 있다."""
    errors = []
    if not NAME_RE.match(str(spec.get("name", ""))):
        errors.append(f"name은 영문 소문자 snake_case: {spec.get('name')!r}")
    if spec.get("kind") not in KINDS:
        return errors + [f"알 수 없는 kind: {spec.get('kind')!r}"]
    if not str(spec.get("description", "")).strip():
        errors.append("description 없음")
    inputs = spec.get("inputs")
    if not isinstance(inputs, list):
        return errors + ["inputs가 목록이 아님"]
    names: list[str] = []
    for inp in inputs:
        n = inp.get("name") if isinstance(inp, dict) else None
        if not n or not NAME_RE.match(n):
            errors.append(f"입력 이름 오류: {inp!r}")
            continue
        names.append(n)
        if inp.get("type") not in INPUT_TYPES:
            errors.append(f"{n}: type은 number 또는 enum")
        if inp.get("type") == "enum" and not inp.get("enum"):
            errors.append(f"{n}: enum 값 목록 없음")
    if len(names) != len(set(names)):
        errors.append("입력 이름 중복")

    if spec["kind"] == "formula":
        steps = spec.get("steps")
        if not isinstance(steps, list) or not steps:
            return errors + ["steps 없음"]
        defined = set(names)
        for step in steps:
            n = step.get("name") if isinstance(step, dict) else None
            if not n or not NAME_RE.match(n):
                errors.append(f"step 이름 오류: {step!r}")
                continue
            if n in defined:
                errors.append(f"이름 중복: {n}")
            try:
                tree = parse(step.get("expr", ""))
            except ToolError as e:
                errors.append(f"{n}: {e}")
                defined.add(n)
                continue
            if undefined := names_in(tree) - defined:
                errors.append(f"{n}: 정의되지 않은 이름 {sorted(undefined)}")
            for table, n_keys in lookups_in(tree):
                if table not in tables:
                    errors.append(f"{n}: 없는 표 lookup(\"{table}\")")
                elif n_keys != len(tables[table]["keys"]):
                    errors.append(f"{n}: lookup(\"{table}\") 키 {len(tables[table]['keys'])}개 필요")
            defined.add(n)
        if spec.get("output") not in {s.get("name") for s in steps if isinstance(s, dict)}:
            errors.append(f"output이 step 이름이 아님: {spec.get('output')!r}")
    else:
        keys = spec.get("keys")
        if not isinstance(keys, list) or not keys:
            return errors + ["keys 없음"]
        if missing := [k for k in keys if k not in names]:
            errors.append(f"keys가 inputs에 없음: {missing}")
        rows = spec.get("rows")
        if not isinstance(rows, list) or not rows:
            return errors + ["rows 없음"]
        seen = set()
        enums = {i["name"]: i.get("enum") for i in inputs if isinstance(i, dict) and i.get("type") == "enum"}
        for row in rows:
            key = row.get("key") if isinstance(row, dict) else None
            if not isinstance(key, dict) or any(k not in key for k in keys):
                errors.append(f"행의 key 누락: {row!r}")
                continue
            if not _is_num(row.get("value")):
                errors.append(f"행의 value가 숫자가 아님: {row!r}")
            sig = tuple(str(key[k]) for k in keys)
            if sig in seen:
                errors.append(f"중복 행: {key}")
            seen.add(sig)
            for k in keys:
                if enums.get(k) and str(key[k]) not in enums[k]:
                    errors.append(f"{k}의 enum에 없는 행 값: {key[k]}")
    return errors


def _matches(got, expect, tolerance=None) -> bool:
    """tolerance가 없으면 문서 값의 자릿수로 반올림해 같은지 본다 (문서 예시는 반올림된 값이다).

    기대값이 참·거짓이면 비교식 결과(bool)와 그대로 맞춘다.
    """
    if isinstance(expect, bool):
        return isinstance(got, bool) and got == expect
    if not (_is_num(got) and _is_num(expect)):
        return False
    if tolerance is not None:
        return abs(got - expect) <= tolerance
    decimals = len(repr(float(expect)).split(".")[1].rstrip("0")) if isinstance(expect, float) else 0
    return _round(got, decimals) == expect


def run_tests(spec: dict, tables: dict) -> list[dict]:
    results = []
    for t in spec.get("tests") or []:
        r = {"input": t.get("input"), "expect": t.get("expect"), "from_doc": bool(t.get("from_doc"))}
        try:
            got = run(spec, t.get("input") or {}, tables)["value"]
            r |= {"got": got, "ok": _matches(got, t.get("expect"), t.get("tolerance", spec.get("tolerance")))}
        except ToolError as e:
            r |= {"got": None, "ok": False, "error": str(e)}
        results.append(r)
    return results
