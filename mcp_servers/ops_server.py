"""계산·이메일 발송 업무 MCP 서버 (이메일은 실제 발송하지 않고 outbox에 기록)."""

import ast
import operator
import uuid
from datetime import datetime

from fastmcp import FastMCP

mcp = FastMCP("ops", instructions="수치 계산과 이메일 발송을 처리하는 MCP 서버")

_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Pow: operator.pow,
    ast.Mod: operator.mod,
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
}

OUTBOX: list[dict] = []


def _eval(node: ast.AST) -> float:
    if isinstance(node, ast.Expression):
        return _eval(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_eval(node.left), _eval(node.right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_eval(node.operand))
    raise ValueError(f"지원하지 않는 수식 요소: {ast.dump(node)}")


@mcp.tool
def calculate(expression: str) -> str:
    """사칙연산 수식을 계산합니다.

    Args:
        expression: 계산할 수식 (예: "50000 * 1.1")
    """
    result = round(_eval(ast.parse(expression.replace(",", ""), mode="eval")), 6)
    if isinstance(result, float) and result.is_integer():
        result = int(result)
    return f"{expression} = {result:,}" if isinstance(result, int) else f"{expression} = {result:,.4f}"


@mcp.tool
def send_email(recipient: str, subject: str, body: str) -> dict:
    """이메일을 발송합니다.

    Args:
        recipient: 수신자 이메일 주소
        subject: 메일 제목
        body: 메일 본문
    """
    message = {
        "message_id": f"MSG-{uuid.uuid4().hex[:8]}",
        "recipient": recipient,
        "subject": subject,
        "body": body,
        "sent_at": datetime.now().isoformat(timespec="seconds"),
        "status": "sent (mock)",
    }
    OUTBOX.append(message)
    return {k: message[k] for k in ("message_id", "recipient", "status", "sent_at")}


if __name__ == "__main__":
    mcp.run(transport="http", host="127.0.0.1", port=8102)
