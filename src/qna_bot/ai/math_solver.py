"""Sub-millisecond safe local math solver for speed-based Telegram Q&A giveaways.

Evaluates pure arithmetic expressions using Python's AST (no eval) with strict
node count and magnitude guards to prevent DoS attacks like 9**9**9.
"""

from __future__ import annotations

import ast
import math
import operator
import re
from collections.abc import Callable

# Strip common giveaway prefixes/suffixes around math expressions
_PREFIX_RE = re.compile(
    r"^(?:(?:q\d*[:.)]?\s*)|(?:question\s*\d*[:.)]?\s*)|(?:math\s*[:.)]?\s*)|"
    r"(?:solve\s*[:.)]?\s*)|(?:calculate\s*[:.)]?\s*)|(?:compute\s*[:.)]?\s*)|"
    r"(?:evaluate\s*[:.)]?\s*)|(?:what\s+is\s+)|(?:what's\s+)|(?:find\s+(?:the\s+value\s+of\s+)?))+",
    re.IGNORECASE,
)
_SUFFIX_RE = re.compile(r"(?:\s*=\s*\?|\s*\?|\s*=|\s*\.)+$")

# Handle simple "X% of Y" questions (e.g., "What is 15% of 240?")
_PERCENT_OF_RE = re.compile(
    r"^(\d+(?:\.\d+)?)\s*%\s*of\s*(\d+(?:\.\d+)?)$",
    re.IGNORECASE,
)

# Require at least one operator (so bare numbers like "2026" or "100" are NOT solved)
_HAS_OPERATOR_RE = re.compile(r"[+\-*/^%]|\bsqrt\s*\(")

_ALLOWED_BINOPS: dict[type[ast.operator], Callable[[float, float], float]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}

_ALLOWED_UNARYOPS: dict[type[ast.unaryop], Callable[[float], float]] = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}

_MAX_AST_NODES = 40
_MAX_ABS_VALUE = 1e15
_MAX_EXPONENT = 100


def _normalize_expression(text: str) -> str:
    expr = text.strip()
    expr = _PREFIX_RE.sub("", expr).strip()
    expr = _SUFFIX_RE.sub("", expr).strip()

    # Check "X% of Y"
    pct_match = _PERCENT_OF_RE.match(expr)
    if pct_match:
        pct, base = pct_match.group(1), pct_match.group(2)
        return f"({pct} / 100) * {base}"

    # Normalize unicode math operators and common verbal forms
    replacements = [
        ("×", "*"),
        ("✕", "*"),
        ("✖", "*"),
        ("÷", "/"),
        ("−", "-"),
        ("–", "-"),
        ("—", "-"),
        ("√", "sqrt"),
    ]
    for old, new in replacements:
        expr = expr.replace(old, new)

    # Replace standalone 'x' or 'X' between numbers/parens with '*'
    expr = re.sub(r"(?<=[\d)])\s*[xX]\s*(?=[\d(])", " * ", expr)
    # Replace '^' with '**' for exponentiation
    expr = expr.replace("^", "**")
    # Convert sqrt144 -> sqrt(144)
    expr = re.sub(r"\bsqrt\s*(\d+(?:\.\d+)?)", r"sqrt(\1)", expr)
    # Remove thousands commas inside numbers like 1,000 + 2,500
    expr = re.sub(r"(?<=\d),(?=\d{3}\b)", "", expr)

    return expr.strip()


def _safe_eval_node(node: ast.AST) -> float:
    if isinstance(node, ast.Expression):
        return _safe_eval_node(node.body)

    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        if isinstance(node.value, bool):
            raise ValueError("Booleans are not allowed")
        val = float(node.value)
        if abs(val) > _MAX_ABS_VALUE:
            raise ValueError("Number too large")
        return val

    if isinstance(node, ast.UnaryOp) and type(node.op) in _ALLOWED_UNARYOPS:
        return _ALLOWED_UNARYOPS[type(node.op)](_safe_eval_node(node.operand))

    if isinstance(node, ast.BinOp) and type(node.op) in _ALLOWED_BINOPS:
        left = _safe_eval_node(node.left)
        right = _safe_eval_node(node.right)
        if isinstance(node.op, ast.Pow):
            if abs(right) > _MAX_EXPONENT or abs(left) > 1e6:
                raise ValueError("Exponent too large")
        if isinstance(node.op, (ast.Div, ast.FloorDiv, ast.Mod)) and right == 0:
            raise ValueError("Division by zero")
        result = _ALLOWED_BINOPS[type(node.op)](left, right)
        if not math.isfinite(result) or abs(result) > _MAX_ABS_VALUE:
            raise ValueError("Result out of bounds")
        return float(result)

    if isinstance(node, ast.Call):
        if (
            isinstance(node.func, ast.Name)
            and node.func.id.lower() == "sqrt"
            and len(node.args) == 1
            and not node.keywords
        ):
            arg = _safe_eval_node(node.args[0])
            if arg < 0:
                raise ValueError("Negative sqrt")
            return math.sqrt(arg)
        raise ValueError("Unsupported function call")

    raise ValueError(f"Unsupported AST node: {type(node).__name__}")


def _format_number(val: float) -> str:
    if math.isclose(val, round(val), rel_tol=0.0, abs_tol=1e-9):
        return str(int(round(val)))
    # Format cleanly up to 6 decimal places without trailing zeros
    return f"{val:.6f}".rstrip("0").rstrip(".")


def try_solve_math(text: str) -> str | None:
    """Return the formatted numeric answer if `text` is a pure arithmetic question, else None."""
    if not text or len(text) > 160:
        return None

    expr = _normalize_expression(text)
    if not expr or len(expr) > 120:
        return None

    # Must contain only digits, math operators, parens, whitespace, dots, and optionally 'sqrt'
    stripped_funcs = re.sub(r"\bsqrt\b", "", expr, flags=re.IGNORECASE)
    if not re.fullmatch(r"[\d\s+\-*/().%]+", stripped_funcs):
        return None

    # Require at least one operator so standalone numbers ("50", "2026") aren't treated as math
    if not _HAS_OPERATOR_RE.search(expr):
        return None

    # Guard against phone-number-like or date-like strings: "2026-11-22" or "+251-912-345"
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", expr) or re.fullmatch(r"\d{1,2}/\d{1,2}/\d{2,4}", expr):
        return None

    try:
        tree = ast.parse(expr, mode="eval")
        if sum(1 for _ in ast.walk(tree)) > _MAX_AST_NODES:
            return None
        value = _safe_eval_node(tree)
        return _format_number(value)
    except Exception:
        return None
