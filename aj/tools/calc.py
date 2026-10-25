"""Exact arithmetic. A 4B model does sums in its head and gets them wrong: with the v5
personality, "what's 17 times 23?" came back "one hundred and seventy-one" (it's 391, measured
2026-10-21). So sums go to calculate(), which works them out exactly.

Only arithmetic is evaluated: numbers, + - * / // % **, brackets and sqrt(), read with Python's
parser and walked node by node. No names, no calls but sqrt, nothing else runs.
"""
import ast
import math
import operator
import re
from collections.abc import Callable

from aj.tools.registry import Level, ToolError, tool

_OPS: dict[type, Callable] = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
                              ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod,
                              ast.Pow: operator.pow}
_UNARY: dict[type, Callable] = {ast.UAdd: operator.pos, ast.USub: operator.neg}
_MAX = 10 ** 30


def evaluate(expression: str) -> float | int:
    """The value of a plain arithmetic expression. Raises ToolError for anything else."""
    text = expression.replace("×", "*").replace("÷", "/").replace("^", "**").replace(",", "")
    try:
        tree = ast.parse(text, mode="eval")
    except SyntaxError:
        raise ToolError(f"I can't work out '{expression}': it isn't a sum I can read.") from None

    def walk(node: ast.AST) -> float | int:
        if isinstance(node, ast.Expression):
            return walk(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
            return node.value
        if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY:
            return _UNARY[type(node.op)](walk(node.operand))
        if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
            left, right = walk(node.left), walk(node.right)
            if isinstance(node.op, ast.Pow) and (abs(right) > 100 or abs(left) > 10 ** 6):
                raise ToolError("That power is too big to work out.")
            try:
                value = _OPS[type(node.op)](left, right)
            except ZeroDivisionError:
                raise ToolError("That divides by zero, which has no answer.") from None
            if isinstance(value, complex) or abs(value) > _MAX:
                raise ToolError("That number is too big to say.")
            return value
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "sqrt" \
                and len(node.args) == 1 and not node.keywords:
            value = walk(node.args[0])
            if value < 0:
                raise ToolError("A negative number has no real square root.")
            return math.sqrt(value)
        raise ToolError(f"I can only work out plain arithmetic, and '{expression}' isn't.")

    return walk(tree)


def say_number(value: float | int) -> str:
    """391, 0.3333, 1.5: whole numbers stay whole, others get at most four decimals."""
    if isinstance(value, float) and value.is_integer() and abs(value) < 1e15:
        value = int(value)
    if isinstance(value, int):
        return f"{value:,}"
    return f"{value:,.4f}".rstrip("0").rstrip(".")


@tool(level=Level.SAFE, timeout=2.0, cloud=False)
def calculate(expression: str) -> str:
    """Work out arithmetic exactly. Use it for every sum; never do sums in your head.

    expression: plain arithmetic, e.g. "17 * 23", "15 / 100 * 80", "sqrt(144)", "(4 + 6) ** 2"
    """
    return f"It's {say_number(evaluate(expression))}."


# ---- spoken sums as instant commands ("what's 17 times 23", "15 percent of 80")

_WORDS = [(r"\bmultiplied by\b|\btimes\b|(?<=\d) x (?=\d)", "*"), (r"\bdivided by\b|\bover\b", "/"),
          (r"\bplus\b|\badd\b", "+"), (r"\bminus\b|\btake away\b", "-"), (r"\bto the power of\b", "**"),
          (r"\bsquared\b", "**2"), (r"\bcubed\b", "**3"), (r"\bmod(?:ulo)?\b", "%"),
          (r"\b(?:percent|per cent) of\b", "/100*")]
_ROOT = re.compile(r"\bsquare root of (\d+(?:\.\d+)?)")
_SUM = re.compile(r"[\d\s.()+\-*/%]+")


def spoken_sum(text: str) -> str | None:
    """'17 times 23' -> '17 * 23'; None unless the whole thing is arithmetic with an operator."""
    expr = _ROOT.sub(r"sqrt(\1)", text)
    for pattern, op in _WORDS:
        expr = re.sub(pattern, f" {op} ", expr)
    bare = expr.replace("sqrt", "")
    if not _SUM.fullmatch(bare) or not re.search(r"\d", bare) or not (re.search(r"[+\-*/%]", bare) or "sqrt" in expr):
        return None
    return " ".join(expr.split())


def _sum(m: re.Match) -> dict | None:
    expr = spoken_sum(m["expr"])
    return {"expression": expr} if expr else None


INTENTS: list[tuple[str, str, Callable[[re.Match], dict | None]]] = [
    (r"(?:(?:what(?:s| s| is)|how much is|calculate|work out|whats the answer to) )?(?P<expr>.+?)", "calculate", _sum),
]
