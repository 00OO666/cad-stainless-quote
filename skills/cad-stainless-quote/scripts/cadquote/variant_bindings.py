"""Target-free CAD binding for lettered casework variants.

Interior packages commonly draw several related counters or cabinets as ``A``,
``B`` and ``C`` variants.  A material-only candidate bucket collapses those
physical objects and can silently reuse the first variant's dimensions.  This
module binds a row suffix to matching CAD view titles, then requires independent
native dimensions across plan and elevation views.

The result is always REVIEW.  It supplies auditable candidates; it does not
confirm the row inventory, unit calibration, or a commercial takeoff item.
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections import defaultdict
from collections.abc import Mapping, Sequence
from typing import Any

CASEWORK_TERMS = (
    "服务台",
    "接待台",
    "收银台",
    "操作台",
    "吧台",
    "柜台",
    "展示柜",
    "取餐柜",
    "COUNTER",
    "RECEPTION",
    "BAR",
    "CABINET",
)
FORBIDDEN_TASK_KEYS = frozenset(
    {"engineering_quantity", "length_mm", "quantity", "unfolded_spec", "width_mm"}
)
VIEW_RE = re.compile(
    r"^(?P<base>.+?)(?P<variant>[A-Z])"
    r"(?P<view>平面图|正立面图|背立面图|侧立面图|立面图|PLAN|FRONT|BACK|SIDE|ELEVATION)$",
    re.IGNORECASE,
)
ROW_VARIANT_RE = re.compile(
    r"^(?P<base>.+?)[\s\-_—–]*[（(]?(?P<variant>[A-Z])[）)]?$",
    re.IGNORECASE,
)


def _compact_text(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).upper()
    return re.sub(r"[\s\-_—–·:：/\\（）()\[\]]+", "", text)


def _title_without_scale(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).upper()
    text = re.sub(r"\s*SCALE\s*[:：].*$", "", text, flags=re.IGNORECASE)
    return re.sub(r"[\s\-_—–·:：/\\（）()\[\]]+", "", text)


def parse_row_variant(name: Any) -> tuple[str, str] | None:
    """Return normalized component base and suffix letter for casework rows."""

    text = unicodedata.normalize("NFKC", str(name or "")).strip().upper()
    matched = ROW_VARIANT_RE.fullmatch(text)
    if matched is None:
        return None
    base = _compact_text(matched.group("base"))
    if not any(term in base for term in CASEWORK_TERMS):
        return None
    return base, matched.group("variant").upper()


def parse_panel_title(title: Any) -> tuple[str, str, str] | None:
    """Parse an exact lettered plan/elevation subview title."""

    matched = VIEW_RE.fullmatch(_title_without_scale(title))
    if matched is None:
        return None
    return (
        _compact_text(matched.group("base")),
        matched.group("variant").upper(),
        matched.group("view").upper(),
    )


def _related_base(row_base: str, panel_base: str) -> bool:
    return len(panel_base) >= 2 and (panel_base in row_base or row_base in panel_base)


def _panel_has_material(
    panel_id: str,
    material_code: str,
    entities_by_panel: Mapping[str, Sequence[Mapping[str, Any]]],
) -> bool:
    expected = _compact_text(material_code)
    return bool(expected) and any(
        _compact_text(entity.get("text")) == expected
        for entity in entities_by_panel.get(panel_id, [])
    )


def _dimension_axis(entity: Mapping[str, Any]) -> str | None:
    if entity.get("entity_type") != "DIMENSION":
        return None
    geometry = entity.get("geometry")
    if not isinstance(geometry, Mapping):
        return None
    point2 = geometry.get("defpoint2")
    point3 = geometry.get("defpoint3")
    if not (
        isinstance(point2, Sequence)
        and not isinstance(point2, str | bytes)
        and len(point2) >= 2
        and isinstance(point3, Sequence)
        and not isinstance(point3, str | bytes)
        and len(point3) >= 2
    ):
        return None
    try:
        delta_x = abs(float(point3[0]) - float(point2[0]))
        delta_y = abs(float(point3[1]) - float(point2[1]))
    except (TypeError, ValueError):
        return None
    if delta_x >= 3.0 * max(delta_y, 1e-9):
        return "horizontal"
    if delta_y >= 3.0 * max(delta_x, 1e-9):
        return "vertical"
    return None


def _valid_native_dimension(entity: Mapping[str, Any]) -> bool:
    if _dimension_axis(entity) is None:
        return False
    geometry = entity.get("geometry")
    if not isinstance(geometry, Mapping):
        return False
    try:
        value = float(entity.get("value"))
        display = float(geometry.get("display_measurement"))
        geometric = float(geometry.get("geometric_measurement"))
    except (TypeError, ValueError):
        return False
    if not math.isfinite(value) or value <= 0:
        return False
    tolerance = max(0.5, value * 0.001)
    return bool(
        abs(value - display) <= tolerance
        and abs(value - geometric) <= tolerance
        and geometry.get("original_entity_id")
        and geometry.get("panel_viewport_handle")
    )


def _nominal(value: float) -> float:
    rounded = round(value)
    if abs(value - rounded) <= max(0.5, abs(value) * 0.001):
        return float(rounded)
    return round(value, 1)


def _dimension_record(
    entity: Mapping[str, Any], panel: Mapping[str, Any]
) -> dict[str, Any]:
    geometry = entity.get("geometry")
    assert isinstance(geometry, Mapping)
    value = float(entity["value"])
    return {
        "panel_id": panel["id"],
        "viewport_handle": panel.get("viewport_handle"),
        "drawing_number": panel.get("drawing_number"),
        "panel_title": panel.get("title"),
        "panel_kind": panel.get("kind"),
        "source_file_id": panel.get("source_file_id"),
        "dimension_entity_id": entity.get("id"),
        "dimension_handle": entity.get("handle"),
        "original_entity_id": geometry.get("original_entity_id"),
        "axis": _dimension_axis(entity),
        "native_value": value,
        "nominal_mm": _nominal(value),
        "units": geometry.get("units"),
        "space": entity.get("space"),
        "bbox": entity.get("bbox"),
    }


def _cluster(records: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    clusters: list[list[dict[str, Any]]] = []
    for record in sorted(records, key=lambda item: item["native_value"]):
        for cluster in clusters:
            center = sum(item["native_value"] for item in cluster) / len(cluster)
            if abs(record["native_value"] - center) <= max(2.0, center * 0.005):
                cluster.append(record)
                break
        else:
            clusters.append([record])
    output: list[dict[str, Any]] = []
    for members in clusters:
        mean = sum(item["native_value"] for item in members) / len(members)
        output.append(
            {
                "native_mean": mean,
                "nominal_mm": _nominal(mean),
                "handle_count": len(
                    {
                        item["dimension_handle"]
                        for item in members
                        if item["dimension_handle"]
                    }
                ),
                "panel_count": len({item["panel_id"] for item in members}),
                "members": members,
            }
        )
    return output


def _panel_dimensions(
    panel: Mapping[str, Any],
    entities_by_panel: Mapping[str, Sequence[Mapping[str, Any]]],
) -> list[dict[str, Any]]:
    return [
        _dimension_record(entity, panel)
        for entity in entities_by_panel.get(str(panel["id"]), [])
        if _valid_native_dimension(entity)
    ]


def _plan_width(
    plan: Mapping[str, Any],
    elevations: Sequence[Mapping[str, Any]],
    entities_by_panel: Mapping[str, Sequence[Mapping[str, Any]]],
) -> tuple[dict[str, Any] | None, list[str]]:
    horizontal = [
        item
        for item in _panel_dimensions(plan, entities_by_panel)
        if item["axis"] == "horizontal" and item["native_value"] >= 200
    ]
    if not horizontal:
        return None, ["NO_PLAN_HORIZONTAL_DIMENSION"]
    selected = max(_cluster(horizontal), key=lambda item: item["native_mean"])
    corroborating = [
        item
        for panel in elevations
        for item in _panel_dimensions(panel, entities_by_panel)
        if item["axis"] == "horizontal"
        and abs(item["native_value"] - selected["native_mean"])
        <= max(5.0, selected["native_mean"] * 0.005)
    ]
    blockers: list[str] = []
    if selected["handle_count"] < 2:
        blockers.append("PLAN_WIDTH_NOT_DUPLICATE_HANDLE_CORROBORATED")
    if not corroborating:
        blockers.append("PLAN_WIDTH_NOT_ELEVATION_CORROBORATED")
    else:
        selected["members"].extend(corroborating)
        selected["handle_count"] = len(
            {
                item["dimension_handle"]
                for item in selected["members"]
                if item["dimension_handle"]
            }
        )
        selected["panel_count"] = len(
            {item["panel_id"] for item in selected["members"]}
        )
    return selected, blockers


def _plan_depth(
    plan: Mapping[str, Any],
    entities_by_panel: Mapping[str, Sequence[Mapping[str, Any]]],
) -> tuple[dict[str, Any] | None, list[str]]:
    vertical = [
        item
        for item in _panel_dimensions(plan, entities_by_panel)
        if item["axis"] == "vertical" and item["native_value"] >= 200
    ]
    if not vertical:
        return None, ["NO_PLAN_VERTICAL_DIMENSION"]
    selected = max(_cluster(vertical), key=lambda item: item["native_mean"])
    blockers = (
        []
        if selected["handle_count"] >= 2
        else ["PLAN_DEPTH_NOT_DUPLICATE_HANDLE_CORROBORATED"]
    )
    return selected, blockers


def _body_height(
    elevations: Sequence[Mapping[str, Any]],
    depth: float,
    entities_by_panel: Mapping[str, Sequence[Mapping[str, Any]]],
) -> tuple[dict[str, Any] | None, list[str]]:
    vertical = [
        item
        for panel in elevations
        for item in _panel_dimensions(panel, entities_by_panel)
        if item["axis"] == "vertical" and depth * 0.75 <= item["native_value"] <= depth * 1.5
    ]
    if not vertical:
        return None, ["NO_REPEATED_PLAUSIBLE_ELEVATION_HEIGHT"]
    selected = max(
        _cluster(vertical),
        key=lambda item: (
            item["handle_count"],
            item["panel_count"],
            item["native_mean"],
        ),
    )
    blockers = (
        []
        if selected["handle_count"] >= 2
        else ["HEIGHT_NOT_DUPLICATE_HANDLE_CORROBORATED"]
    )
    return selected, blockers


def _is_plan(view: str) -> bool:
    return view in {"平面图", "PLAN"}


def _is_elevation(view: str) -> bool:
    return "立面图" in view or view in {"FRONT", "BACK", "SIDE", "ELEVATION"}


def _format_dimension(value: float) -> str:
    return str(int(value)) if value.is_integer() else f"{value:g}"


def build_variant_binding_row(
    task: Mapping[str, Any],
    panels_by_id: Mapping[str, Mapping[str, Any]],
    entities_by_panel: Mapping[str, Sequence[Mapping[str, Any]]],
) -> dict[str, Any]:
    """Build one fail-closed REVIEW candidate from target-free inputs."""

    result: dict[str, Any] = {
        "sequence": int(task["sequence"]),
        "name": task.get("name"),
        "material_code": task.get("material_code"),
        "eligible": False,
        "binding_state": "REVIEW",
        "prediction": None,
        "reason_codes": [],
        "audit": {},
    }
    parsed_row = parse_row_variant(task.get("name"))
    if parsed_row is None:
        result["reason_codes"] = ["NOT_LETTERED_CASEWORK_VARIANT"]
        return result
    row_base, variant = parsed_row
    result["eligible"] = True
    result["audit"]["row_variant"] = {"base": row_base, "variant": variant}

    candidate_ids = {
        str(candidate["sheet_id"])
        for candidate in task.get("candidate_occurrences", [])
        if isinstance(candidate, Mapping) and candidate.get("sheet_id")
    }
    matched: list[tuple[Mapping[str, Any], str]] = []
    rejected: list[dict[str, Any]] = []
    for panel_id in sorted(candidate_ids):
        panel = panels_by_id.get(panel_id)
        parsed_title = parse_panel_title(panel.get("title") if panel else None)
        if panel is None or parsed_title is None:
            rejected.append({"panel_id": panel_id, "reason": "NO_VARIANT_VIEW_TITLE"})
            continue
        panel_base, panel_variant, view = parsed_title
        if panel_variant != variant or not _related_base(row_base, panel_base):
            rejected.append(
                {
                    "panel_id": panel_id,
                    "title": panel.get("title"),
                    "reason": "VARIANT_OR_COMPONENT_BASE_MISMATCH",
                }
            )
            continue
        if not _panel_has_material(
            panel_id, str(task.get("material_code") or ""), entities_by_panel
        ):
            rejected.append(
                {
                    "panel_id": panel_id,
                    "title": panel.get("title"),
                    "reason": "ROW_MATERIAL_CODE_NOT_PRINTED_IN_PANEL",
                }
            )
            continue
        matched.append((panel, view))

    plans = [panel for panel, view in matched if _is_plan(view)]
    elevations = [panel for panel, view in matched if _is_elevation(view)]
    result["audit"].update(
        {
            "candidate_panel_ids": sorted(candidate_ids),
            "matched_panels": [
                {
                    "panel_id": panel["id"],
                    "title": panel.get("title"),
                    "drawing_number": panel.get("drawing_number"),
                    "viewport_handle": panel.get("viewport_handle"),
                    "source_file_id": panel.get("source_file_id"),
                    "view_role": view,
                }
                for panel, view in matched
            ],
            "rejected_panels": rejected,
        }
    )
    if len(plans) != 1:
        result["reason_codes"] = ["PLAN_VARIANT_PANEL_NOT_UNIQUE"]
        return result
    if not elevations:
        result["reason_codes"] = ["NO_VARIANT_ELEVATION_PANEL"]
        return result

    width, width_blockers = _plan_width(plans[0], elevations, entities_by_panel)
    depth, depth_blockers = _plan_depth(plans[0], entities_by_panel)
    if width is None or depth is None:
        result["reason_codes"] = [*width_blockers, *depth_blockers]
        return result
    height, height_blockers = _body_height(
        elevations, float(depth["native_mean"]), entities_by_panel
    )
    if height is None:
        result["reason_codes"] = [
            *width_blockers,
            *depth_blockers,
            *height_blockers,
        ]
        return result
    blockers = [*width_blockers, *depth_blockers, *height_blockers]
    roles = {
        "footprint_width": width,
        "body_height": height,
        "footprint_depth": depth,
    }
    result["audit"]["dimension_roles"] = roles
    if blockers:
        result["reason_codes"] = blockers
        return result

    values = tuple(float(roles[role]["nominal_mm"]) for role in roles)
    units = {
        str(member.get("units") or "").strip().lower()
        for role in roles.values()
        for member in role["members"]
    }
    unit_reason = (
        "NATIVE_DIMENSION_UNITS_PRESENT"
        if units and units <= {"millimeters", "millimeter", "mm"}
        else "UNIT_CALIBRATION_REQUIRED"
    )
    reason_codes = [
        "EXACT_COMPONENT_VARIANT_TITLE_BINDING",
        "ROW_MATERIAL_CODE_PRESENT_IN_SELECTED_VIEWS",
        "MULTI_VIEW_NATIVE_DIMENSION_CORROBORATION",
        unit_reason,
        "REAL_CROSS_PROJECT_TRANSFER_NOT_YET_PROVEN",
    ]
    result["binding_state"] = "BOUND_CAD_NATIVE_REVIEW"
    result["prediction"] = {
        "unfolded_spec": "*".join(_format_dimension(value) for value in values),
        "role_order": ["footprint_width", "body_height", "footprint_depth"],
        "width_mm": values[0],
        "height_mm": values[1],
        "depth_mm": values[2],
        "state": "REVIEW",
        "reason_codes": reason_codes,
    }
    result["reason_codes"] = reason_codes
    return result


def build_variant_bindings(
    task_payload: Mapping[str, Any], panel_payload: Mapping[str, Any]
) -> dict[str, Any]:
    """Build all lettered-variant candidates and reject target-contaminated tasks."""

    tasks = task_payload.get("tasks", [])
    if not isinstance(tasks, list):
        raise ValueError("tasks must be an array")
    contaminated = [
        int(task.get("sequence", -1))
        for task in tasks
        if isinstance(task, Mapping) and FORBIDDEN_TASK_KEYS.intersection(task)
    ]
    if contaminated:
        raise ValueError(
            "task input contains forbidden numeric target keys for sequences "
            + ", ".join(map(str, contaminated))
        )
    sheets = panel_payload.get("sheets", [])
    entities = panel_payload.get("entities", [])
    if not isinstance(sheets, list) or not isinstance(entities, list):
        raise ValueError("panel catalog sheets/entities must be arrays")
    panels_by_id = {
        str(panel["id"]): panel
        for panel in sheets
        if isinstance(panel, Mapping) and panel.get("id")
    }
    entities_by_panel: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for entity in entities:
        if isinstance(entity, Mapping) and entity.get("sheet_id"):
            entities_by_panel[str(entity["sheet_id"])].append(entity)
    rows = [
        build_variant_binding_row(task, panels_by_id, entities_by_panel)
        for task in sorted(tasks, key=lambda item: int(item["sequence"]))
        if isinstance(task, Mapping)
    ]
    return {
        "schema_version": "variant-title-physical-binding/1.0",
        "mode": "TARGET_FREE_CAD_NATIVE_VARIANT_BINDING",
        "production_eligible": False,
        "gold_accessed_during_build": False,
        "warning": (
            "Candidates are REVIEW-only until physical row ownership, unit calibration, "
            "and real cross-project transfer are independently verified."
        ),
        "invariants": [
            "No H/J/K/L target is accepted in a task input.",
            "The suffix letter must match an exact CAD component-view title.",
            "Every selected panel must print the row material code.",
            "Every role requires independent native DIMENSION handles.",
            "Ambiguous or incomplete evidence remains REVIEW.",
        ],
        "summary": {
            "row_count": len(rows),
            "eligible_count": sum(bool(row["eligible"]) for row in rows),
            "bound_count": sum(row["prediction"] is not None for row in rows),
            "release_pass_count": 0,
        },
        "rows": rows,
    }


__all__ = [
    "build_variant_binding_row",
    "build_variant_bindings",
    "parse_panel_title",
    "parse_row_variant",
]
