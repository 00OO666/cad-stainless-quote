"""Deterministic expression, quantity, and amount calculations."""

from __future__ import annotations

import ast
import operator
from decimal import ROUND_HALF_UP, Decimal, DecimalException, InvalidOperation

from .models import ReviewStatus, TakeoffItem

_BINARY = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
}
_UNARY = {ast.UAdd: operator.pos, ast.USub: operator.neg}
_ENGINEERING_VARIABLES = frozenset({"width_mm", "length_mm", "quantity"})
_MAX_ABSOLUTE_NUMBER = Decimal("1e15")
_MAX_LITERAL = Decimal("1e12")


def _expression_tree(expression: str, *, label: str) -> ast.Expression:
    text = expression.strip().replace("×", "*").replace("÷", "/")
    if not text:
        raise ValueError(f"empty {label}")
    if len(text) > 256:
        raise ValueError(f"{label} is too long")
    try:
        tree = ast.parse(text, mode="eval")
    except SyntaxError as exc:
        raise ValueError(f"invalid {label}: {expression!r}") from exc
    if sum(1 for _ in ast.walk(tree)) > 64:
        raise ValueError(f"{label} is too complex")
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and not isinstance(node.value, bool)
            and isinstance(node.value, (int, float))
        ):
            literal = Decimal(str(node.value))
            if not literal.is_finite() or abs(literal) > _MAX_LITERAL:
                raise ValueError(f"{label} contains an unsafe numeric literal")
    return tree


def _walk_arithmetic(
    node: ast.AST,
    *,
    expression: str,
    variables: dict[str, Decimal] | None = None,
) -> Decimal:
    def checked(value: Decimal) -> Decimal:
        if not value.is_finite() or abs(value) > _MAX_ABSOLUTE_NUMBER:
            raise ValueError("numeric expression exceeds the supported finite range")
        return value

    if isinstance(node, ast.Expression):
        return _walk_arithmetic(node.body, expression=expression, variables=variables)
    if (
        isinstance(node, ast.Constant)
        and not isinstance(node.value, bool)
        and isinstance(node.value, (int, float))
    ):
        return checked(Decimal(str(node.value)))
    if isinstance(node, ast.Name) and variables is not None and node.id in variables:
        return checked(variables[node.id])
    if isinstance(node, ast.BinOp) and type(node.op) in _BINARY:
        left = _walk_arithmetic(node.left, expression=expression, variables=variables)
        right = _walk_arithmetic(node.right, expression=expression, variables=variables)
        if isinstance(node.op, ast.Div) and right == 0:
            raise ValueError("division by zero")
        return checked(_BINARY[type(node.op)](left, right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY:
        return checked(
            _UNARY[type(node.op)](
                _walk_arithmetic(node.operand, expression=expression, variables=variables)
            )
        )
    raise ValueError(f"unsupported numeric expression: {expression!r}")


def evaluate_numeric_expression(expression: str | int | float | Decimal) -> Decimal:
    """Evaluate basic arithmetic without Python eval or names/functions."""
    if isinstance(expression, bool):
        raise ValueError("boolean is not a numeric expression")
    if isinstance(expression, (Decimal, int, float)):
        value = expression if isinstance(expression, Decimal) else Decimal(str(expression))
        if not value.is_finite() or abs(value) > _MAX_ABSOLUTE_NUMBER:
            raise ValueError("numeric expression exceeds the supported finite range")
        return value
    text = str(expression)
    tree = _expression_tree(text, label="numeric expression")
    try:
        return _walk_arithmetic(tree, expression=text)
    except (InvalidOperation, TypeError) as exc:
        raise ValueError(f"invalid numeric expression: {expression!r}") from exc


def evaluate_engineering_quantity_expression(
    expression: str,
    *,
    unit: str,
    width_mm: float | None,
    length_mm: float | None,
    quantity: float | None,
) -> Decimal:
    """Evaluate an audited item-level quantity expression with fixed variables only."""

    tree = _expression_tree(expression, label="engineering quantity expression")
    validate_engineering_quantity_expression(expression, unit=unit)
    referenced = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    unsupported = sorted(referenced - _ENGINEERING_VARIABLES)
    if unsupported:
        raise ValueError(
            "unsupported engineering quantity variable(s): " + ", ".join(unsupported)
        )
    raw_values = {
        "width_mm": width_mm,
        "length_mm": length_mm,
        "quantity": quantity,
    }
    missing = sorted(name for name in referenced if raw_values[name] is None)
    if missing:
        raise ValueError("missing engineering quantity input(s): " + ", ".join(missing))
    if unit in {"m", "㎡"} and quantity != 1 and "quantity" not in referenced:
        raise ValueError(
            "engineering quantity expression must reference quantity when visible quantity is not 1"
        )
    variables = {
        name: Decimal(str(value))
        for name, value in raw_values.items()
        if value is not None
    }
    try:
        result = _walk_arithmetic(tree, expression=expression, variables=variables)
    except (InvalidOperation, TypeError) as exc:
        raise ValueError(f"invalid engineering quantity expression: {expression!r}") from exc
    if not result.is_finite() or result <= 0 or result > _MAX_ABSOLUTE_NUMBER:
        raise ValueError(
            "engineering quantity expression must return a bounded finite positive value"
        )
    return result


def validate_engineering_quantity_expression(expression: str, *, unit: str) -> None:
    """Enforce dimensions and explicit millimetre conversion for the billing unit."""

    tree = _expression_tree(expression, label="engineering quantity expression")
    names = [node.id for node in ast.walk(tree) if isinstance(node, ast.Name)]
    unsupported = sorted(set(names) - _ENGINEERING_VARIABLES)
    if unsupported:
        raise ValueError(
            "unsupported engineering quantity variable(s): " + ", ".join(unsupported)
        )
    if not names:
        raise ValueError("engineering quantity expression must reference a CAD-backed field")

    expected_degree = {"m": 1, "㎡": 2, "件": 0, "套": 0}.get(unit)
    if expected_degree is None:
        raise ValueError(f"unsupported engineering quantity unit: {unit!r}")

    body: ast.AST = tree.body
    if unit in {"m", "㎡"}:
        divisor = Decimal("1000") if unit == "m" else Decimal("1000000")
        if not (
            isinstance(body, ast.BinOp)
            and isinstance(body.op, ast.Div)
            and isinstance(body.right, ast.Constant)
            and not isinstance(body.right.value, bool)
            and Decimal(str(body.right.value)) == divisor
        ):
            raise ValueError(
                f"{unit} engineering quantity expression must end with /{divisor}"
            )
        numerator = body.left
        if any(isinstance(node, ast.Div) for node in ast.walk(numerator)):
            raise ValueError("engineering quantity numerator cannot contain another division")
    else:
        numerator = body
        if any(isinstance(node, (ast.Add, ast.Sub, ast.Div)) for node in ast.walk(numerator)):
            raise ValueError(f"{unit} engineering quantity supports quantity multipliers only")

    for node in ast.walk(numerator):
        if (
            isinstance(node, ast.Constant)
            and not isinstance(node.value, bool)
            and isinstance(node.value, (int, float))
        ):
            factor = Decimal(str(node.value))
            if factor != factor.to_integral_value() or not Decimal("1") <= factor <= Decimal("100"):
                raise ValueError(
                    "engineering quantity topology multipliers must be integers from 1 to 100"
                )

    dimensions = {"width_mm": 1, "length_mm": 1, "quantity": 0}

    def degree(node: ast.AST) -> int:
        if isinstance(node, ast.Name) and node.id in dimensions:
            return dimensions[node.id]
        if (
            isinstance(node, ast.Constant)
            and not isinstance(node.value, bool)
            and isinstance(node.value, (int, float))
        ):
            return 0
        if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY:
            return degree(node.operand)
        if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub)):
            left, right = degree(node.left), degree(node.right)
            if left != right:
                raise ValueError("engineering quantity addition/subtraction mixes dimensions")
            return left
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mult):
            return degree(node.left) + degree(node.right)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            return degree(node.left) - degree(node.right)
        raise ValueError(f"unsupported engineering quantity expression: {expression!r}")

    actual_degree = degree(numerator)
    if actual_degree != expected_degree:
        raise ValueError(
            f"engineering quantity dimension does not match unit {unit}: "
            f"expected L^{expected_degree}, got L^{actual_degree}"
        )
    referenced = set(names)
    quantity_nodes = [
        node for node in ast.walk(numerator) if isinstance(node, ast.Name) and node.id == "quantity"
    ]
    if len(quantity_nodes) > 1:
        raise ValueError("engineering quantity may reference quantity at most once")
    parents = {
        child: parent
        for parent in ast.walk(numerator)
        for child in ast.iter_child_nodes(parent)
    }
    for node in quantity_nodes:
        parent = parents.get(node)
        if node is not numerator and not (
            isinstance(parent, ast.BinOp) and isinstance(parent.op, ast.Mult)
        ):
            raise ValueError("quantity must be a direct positive multiplication factor")
    if unit == "㎡" and not {"width_mm", "length_mm"} <= referenced:
        raise ValueError("㎡ engineering quantity must reference width_mm and length_mm")
    if unit in {"件", "套"} and referenced != {"quantity"}:
        raise ValueError(f"{unit} engineering quantity may reference quantity only")
    if unit in {"件", "套"} and len(quantity_nodes) != 1:
        raise ValueError(f"{unit} engineering quantity must reference quantity exactly once")


def engineering_quantity_expression_to_excel(expression: str, *, row: int) -> str:
    """Compile the safe expression grammar to an Excel formula for one quote row."""

    if row < 1:
        raise ValueError("Excel row must be positive")
    tree = _expression_tree(expression, label="engineering quantity expression")
    cells = {"width_mm": f"I{row}", "length_mm": f"J{row}", "quantity": f"K{row}"}

    def render(node: ast.AST) -> str:
        if isinstance(node, ast.Expression):
            return render(node.body)
        if (
            isinstance(node, ast.Constant)
            and not isinstance(node.value, bool)
            and isinstance(node.value, (int, float))
        ):
            return str(node.value)
        if isinstance(node, ast.Name) and node.id in cells:
            return cells[node.id]
        if isinstance(node, ast.BinOp) and type(node.op) in _BINARY:
            symbol = {ast.Add: "+", ast.Sub: "-", ast.Mult: "*", ast.Div: "/"}[
                type(node.op)
            ]
            return f"({render(node.left)}{symbol}{render(node.right)})"
        if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY:
            symbol = "+" if isinstance(node.op, ast.UAdd) else "-"
            return f"({symbol}{render(node.operand)})"
        raise ValueError(f"unsupported engineering quantity expression: {expression!r}")

    rendered = render(tree)
    referenced = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    unsupported = sorted(referenced - _ENGINEERING_VARIABLES)
    if unsupported:
        raise ValueError(
            "unsupported engineering quantity variable(s): " + ", ".join(unsupported)
        )
    return f"={rendered}"


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
    if not value.is_finite() or abs(value) > _MAX_ABSOLUTE_NUMBER:
        raise ValueError("calculation result is outside the supported finite range")
    try:
        return float(value.quantize(Decimal(places), rounding=ROUND_HALF_UP))
    except DecimalException as exc:
        raise ValueError("calculation result cannot be rounded safely") from exc


def calculate_item(item: TakeoffItem, *, require_price: bool = False) -> TakeoffItem:
    """Calculate one item without promoting its review status.

    Missing calculation inputs make the row BLOCK. A missing price blocks only when
    ``require_price`` is true.
    """
    # Calculated fields are always rebuilt from the current dimensions and
    # price. Never leak a stale imported cache through invalid or incomplete
    # inputs, including a rejected custom basis.
    updates: dict[str, object] = {"engineering_quantity": None, "amount": None}
    unit = item.unit or infer_unit(item.pricing_method)
    if unit is None:
        return item.model_copy(
            update={"status": ReviewStatus.BLOCK, "block_reason": "无法确定计价单位", **updates}
        )
    updates["unit"] = unit

    if item.engineering_quantity_expression:
        if item.quantity is None:
            return item.model_copy(
                update={"status": ReviewStatus.BLOCK, "block_reason": "缺少构件数量", **updates}
            )
        missing_audit: list[str] = []
        if not (item.engineering_quantity_basis or "").strip():
            missing_audit.append("计算依据")
        if not item.engineering_quantity_evidence_ids:
            missing_audit.append("证据ID")
        if missing_audit:
            return item.model_copy(
                update={
                    "status": ReviewStatus.BLOCK,
                    "block_reason": "自定义工程量表达式缺少" + "、".join(missing_audit),
                    **updates,
                }
            )
        try:
            engineering = evaluate_engineering_quantity_expression(
                item.engineering_quantity_expression,
                unit=unit,
                width_mm=item.width_mm,
                length_mm=item.length_mm,
                quantity=item.quantity,
            )
        except ValueError as exc:
            return item.model_copy(
                update={
                    "status": ReviewStatus.BLOCK,
                    "block_reason": f"工程量表达式无效：{exc}",
                    **updates,
                }
            )
    else:
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
                        update={
                            "status": ReviewStatus.BLOCK,
                            "block_reason": "缺少展开宽度",
                            **updates,
                        }
                    )
                try:
                    updates["width_mm"] = _rounded(width)
                except ValueError as exc:
                    return item.model_copy(
                        update={
                            "status": ReviewStatus.BLOCK,
                            "block_reason": f"展开宽度无效：{exc}",
                            **updates,
                        }
                    )
                engineering = width * length * quantity / Decimal("1000000")

    try:
        updates["engineering_quantity"] = _rounded(engineering)
    except ValueError as exc:
        return item.model_copy(
            update={
                "status": ReviewStatus.BLOCK,
                "block_reason": f"工程量计算结果无效：{exc}",
                **updates,
            }
        )
    if item.unit_price is None:
        if require_price:
            updates.update(
                {"status": ReviewStatus.BLOCK, "block_reason": "需要匹配已批准的价格库"}
            )
    else:
        try:
            updates["amount"] = _rounded(
                engineering * Decimal(str(item.unit_price)), "0.01"
            )
        except ValueError as exc:
            return item.model_copy(
                update={
                    "status": ReviewStatus.BLOCK,
                    "block_reason": f"金额计算结果无效：{exc}",
                    **updates,
                }
            )
    return item.model_copy(update=updates)


def calculate_items(
    items: list[TakeoffItem], *, require_price: bool = False
) -> list[TakeoffItem]:
    return [calculate_item(item, require_price=require_price) for item in items]
