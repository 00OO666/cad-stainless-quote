"""Deterministic expression, quantity, and amount calculations."""

from __future__ import annotations

import ast
import operator
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from .models import ReviewStatus, TakeoffItem

_BINARY = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
}
_UNARY = {ast.UAdd: operator.pos, ast.USub: operator.neg}


def evaluate_numeric_expression(expression: str | int | float | Decimal) -> Decimal:
    """Evaluate basic arithmetic without Python eval or names/functions."""
    if isinstance(expression, Decimal):
        return expression
    if isinstance(expression, (int, float)):
        return Decimal(str(expression))
    text = str(expression).strip().replace("×", "*").replace("÷", "/")
    if not text:
        raise ValueError("empty numeric expression")
    try:
        tree = ast.parse(text, mode="eval")
    except SyntaxError as exc:
        raise ValueError(f"invalid numeric expression: {expression!r}") from exc

    def walk(node: ast.AST) -> Decimal:
        if isinstance(node, ast.Expression):
            return walk(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return Decimal(str(node.value))
        if isinstance(node, ast.BinOp) and type(node.op) in _BINARY:
            left, right = walk(node.left), walk(node.right)
            if isinstance(node.op, ast.Div) and right == 0:
                raise ValueError("division by zero")
            return _BINARY[type(node.op)](left, right)
        if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY:
            return _UNARY[type(node.op)](walk(node.operand))
        raise ValueError(f"unsupported numeric expression: {expression!r}")

    try:
        return walk(tree)
    except (InvalidOperation, TypeError) as exc:
        raise ValueError(f"invalid numeric expression: {expression!r}") from exc


def infer_unit(pricing_method: str | None) -> str | None:
    text = (pricing_method or "").lower().replace(" ", "")
    if any(token in text for token in ("按米", "延米", "linear", "permetre", "permeter")):
        return "m"
    if any(token in text for token in ("平方米", "㎡", "面积", "展开", "投影", "persqm")):
        return "㎡"
    if "套" in text or "set" in text:
        return "套"
    if "件" in text or "piece" in text:
        return "件"
    return None


def infer_unfolded_width(spec: str | None) -> Decimal | None:
    if not spec:
        return None
    normalized = spec.replace(" ", "").replace("＋", "+")
    if "+" not in normalized or any(mark in normalized for mark in ("*", "×", "x", "X")):
        return None
    try:
        value = evaluate_numeric_expression(normalized)
    except ValueError:
        return None
    return value if value >= 0 else None


def _rounded(value: Decimal, places: str = "0.000001") -> float:
    return float(value.quantize(Decimal(places), rounding=ROUND_HALF_UP))


def calculate_item(item: TakeoffItem, *, require_price: bool = False) -> TakeoffItem:
    """Calculate one item without promoting its review status.

    Missing calculation inputs make the row BLOCK. A missing price blocks only when
    ``require_price`` is true.
    """
    updates: dict[str, object] = {}
    unit = item.unit or infer_unit(item.pricing_method)
    if unit is None:
        return item.model_copy(
            update={"status": ReviewStatus.BLOCK, "block_reason": "无法确定计价单位"}
        )
    updates["unit"] = unit

    quantity = Decimal(str(item.quantity)) if item.quantity is not None else None
    if quantity is None:
        return item.model_copy(
            update={"status": ReviewStatus.BLOCK, "block_reason": "缺少构件数量", **updates}
        )

    if unit in ("件", "套"):
        engineering = quantity
    else:
        if item.length_mm is None:
            return item.model_copy(
                update={"status": ReviewStatus.BLOCK, "block_reason": "缺少构件长度", **updates}
            )
        length = Decimal(str(item.length_mm))
        if unit == "m":
            engineering = length * quantity / Decimal("1000")
        else:
            width = (
                Decimal(str(item.width_mm))
                if item.width_mm is not None
                else infer_unfolded_width(item.unfolded_spec)
            )
            if width is None:
                return item.model_copy(
                    update={"status": ReviewStatus.BLOCK, "block_reason": "缺少展开宽度", **updates}
                )
            updates["width_mm"] = _rounded(width)
            engineering = width * length * quantity / Decimal("1000000")

    updates["engineering_quantity"] = _rounded(engineering)
    if item.unit_price is None:
        if require_price:
            updates.update(
                {"status": ReviewStatus.BLOCK, "block_reason": "需要匹配已批准的价格库"}
            )
    else:
        updates["amount"] = _rounded(engineering * Decimal(str(item.unit_price)), "0.01")
    return item.model_copy(update=updates)


def calculate_items(
    items: list[TakeoffItem], *, require_price: bool = False
) -> list[TakeoffItem]:
    return [calculate_item(item, require_price=require_price) for item in items]
