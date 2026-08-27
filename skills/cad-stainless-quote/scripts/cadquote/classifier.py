"""Conservative sheet classification from filename and title-block evidence."""

from __future__ import annotations

import math
import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(slots=True)
class ClassificationResult:
    kind: str
    confidence: float
    drawing_number: str | None = None
    title: str | None = None
    evidence: list[str] = field(default_factory=list)
    scores: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "confidence": self.confidence,
            "drawing_number": self.drawing_number,
            "title": self.title,
            "evidence": self.evidence,
            "scores": self.scores,
        }


@dataclass(frozen=True, slots=True)
class _Rule:
    kind: str
    pattern: re.Pattern[str]
    weight: float
    label: str


def _rule(kind: str, pattern: str, weight: float, label: str) -> _Rule:
    return _Rule(kind, re.compile(pattern, re.IGNORECASE), weight, label)


# These are semantic drawing terms, not project-specific sheet positions or labels.
_RULES: tuple[_Rule, ...] = (
    _rule("elevation_index", r"立面(?:图)?索引|索引立面|ELEVATION\s+INDEX", 8.0, "立面索引"),
    _rule("catalog", r"目录|图纸清单|DRAWING\s+(?:LIST|INDEX)", 4.5, "目录"),
    _rule("cover", r"封面|COVER\s+SHEET", 4.0, "封面"),
    _rule(
        "material",
        r"材料表|物料表|材料选型|物料选型|MATERIAL\s+(?:SCHEDULE|BOARD)",
        5.0,
        "材料表",
    ),
    _rule("door", r"门表|门大样|门详图|DOOR\s+(?:SCHEDULE|DETAIL)", 5.0, "门图"),
    _rule(
        "ceiling",
        r"天花|顶面|吊顶|REFLECTED\s+CEILING|CEILING\s+PLAN|\bRCP\b",
        4.5,
        "天花/顶面",
    ),
    _rule("floor", r"地花|地面铺装|地坪|FLOOR\s+(?:FINISH|PATTERN)", 4.5, "地面"),
    _rule("detail", r"节点|大样|详图|剖面|DETAIL|SECTION", 4.2, "节点/大样"),
    _rule("elevation", r"立面图?|ELEVATION", 3.8, "立面"),
    _rule("plan", r"平面图?|总平面|布置图|LAYOUT\s+PLAN|FLOOR\s+PLAN|\bPLAN\b", 3.8, "平面"),
    # Common code conventions are supporting evidence only and cannot classify by themselves.
    _rule("elevation", r"(?<![A-Z0-9])[A-Z]{0,3}-?E-?\d{1,3}(?!\d)", 1.4, "E类图号"),
    _rule("detail", r"(?<![A-Z0-9])[A-Z]{0,3}-?D-?\d{1,3}(?!\d)", 1.4, "D类图号"),
    _rule("plan", r"(?<![A-Z0-9])P-?\d{1,3}(?!\d)", 1.2, "P类图号"),
)

_DRAWING_NUMBER_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"(?<![A-Z0-9])"
        r"([A-Z0-9]{1,4}(?:-[A-Z0-9]{1,4})?-\d{1,3}"
        r"(?:\s*[~～至]\s*(?:[A-Z0-9]{1,4}(?:-[A-Z0-9]{1,4})?-)?\d{1,3})?)"
        r"(?!\d)",
        re.IGNORECASE,
    ),
    re.compile(r"(?:图号|DRAWING\s*(?:NO\.?|NUMBER))\s*[:：]?\s*([A-Z0-9._/-]+)", re.IGNORECASE),
)

_DETAIL_VIEW_RE = re.compile(r"节点|大样|详图|剖面|DETAIL|SECTION", re.IGNORECASE)
_CEILING_SUBJECT_RE = re.compile(
    r"天花|顶面|吊顶|REFLECTED\s+CEILING|CEILING|\bRCP\b",
    re.IGNORECASE,
)
_CEILING_PLAN_RE = re.compile(
    r"(?:天花|顶面|吊顶).{0,16}(?:平面|布置)|"
    r"REFLECTED\s+CEILING(?:\s+PLAN)?|CEILING\s+PLAN|\bRCP\b",
    re.IGNORECASE,
)


def normalize_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value or "")
    normalized = normalized.replace("＿", "_").replace("–", "-").replace("—", "-")
    return re.sub(r"\s+", " ", normalized).strip()


def extract_drawing_number(values: Iterable[str]) -> str | None:
    """Extract a general drawing-number token without assuming a project prefix."""

    for value in values:
        text = normalize_text(value).upper()
        for pattern in _DRAWING_NUMBER_PATTERNS:
            match = pattern.search(text)
            if match:
                candidate = re.sub(r"\s+", "", match.group(1))
                if re.match(r"MT[-_/]", candidate, re.IGNORECASE):
                    continue
                prefix = candidate.split("~", 1)[0].rsplit("-", 1)[0]
                if not re.search(r"[A-Z]", prefix, re.IGNORECASE):
                    continue
                return candidate
    return None


def _candidate_title(title_texts: list[str], filename_stem: str) -> str | None:
    meaningful: list[str] = []
    for value in title_texts:
        text = normalize_text(value)
        if len(text) < 2 or len(text) > 160:
            continue
        if any(rule.pattern.search(text) for rule in _RULES):
            meaningful.append(text)
    if meaningful:
        return min(dict.fromkeys(meaningful), key=lambda text: (len(text), text))
    stem = normalize_text(filename_stem)
    return stem or None


def classify_sheet(
    filename: str | Path,
    title_texts: Iterable[str] = (),
    *,
    layout_name: str | None = None,
    drawing_number: str | None = None,
    primary_title_texts: Iterable[str] = (),
) -> ClassificationResult:
    """Classify one layout/sheet and retain human-readable evidence.

    Filename and title-block text can both contain several drawing categories.
    A close score is therefore returned as ``unknown`` instead of forcing the
    highest category.  Downstream reasoning may later resolve that REVIEW item.
    """

    path = Path(filename)
    file_text = normalize_text(path.stem)
    primary_titles = [
        normalize_text(value) for value in primary_title_texts if normalize_text(value)
    ]
    titles = [normalize_text(value) for value in title_texts if normalize_text(value)]
    sources = [("filename", file_text, 1.0)]
    # A structured title-block field (for example an ATTRIB tagged SHEET_TITLE)
    # is stronger than a nearby fixed glyph such as the bare word DETAIL.  The
    # latter is still retained as supporting evidence, but cannot outvote the
    # human-readable local view title on its own.
    sources.extend(("primary_title", title, 1.5) for title in primary_titles)
    sources.extend(("title", title, 1.0) for title in titles)
    if layout_name:
        sources.append(("layout", normalize_text(layout_name), 0.55))

    scores: dict[str, float] = {}
    evidence_by_kind: dict[str, list[str]] = {}
    matched_keys: set[tuple[str, str, str]] = set()
    for source_name, text, source_weight in sources:
        for rule in _RULES:
            if not text or not rule.pattern.search(text):
                continue
            key = (rule.kind, rule.label, source_name)
            if key in matched_keys:
                continue
            matched_keys.add(key)
            scores[rule.kind] = scores.get(rule.kind, 0.0) + rule.weight * source_weight
            evidence_by_kind.setdefault(rule.kind, []).append(
                f"{source_name}:{rule.label}:{text[:120]}"
            )

    # The word 立面 inside 立面索引 is not separate evidence for an elevation sheet.
    if "elevation_index" in scores and "elevation" in scores:
        for _source_name, text, source_weight in sources:
            if re.search(r"立面(?:图)?索引|ELEVATION\s+INDEX", text, re.IGNORECASE):
                scores["elevation"] = max(0.0, scores["elevation"] - 3.8 * source_weight)

    # Ceiling/floor/door describe a subject, while plan/elevation/detail
    # describe a view type.  With the current one-axis Sheet.kind contract we
    # choose the explicit view type when a ceiling title says 节点/大样/剖面,
    # and keep the ceiling kind only for an actual ceiling plan/RCP.  This also
    # prevents "REFLECTED CEILING PLAN" from tying with the generic PLAN rule.
    for _source_name, text, source_weight in sources:
        if _CEILING_SUBJECT_RE.search(text) and _DETAIL_VIEW_RE.search(text):
            scores["ceiling"] = max(0.0, scores.get("ceiling", 0.0) - 4.5 * source_weight)
        if _CEILING_PLAN_RE.search(text) and "plan" in scores:
            scores["plan"] = max(0.0, scores["plan"] - 3.8 * source_weight)

    # Door/floor details retain their existing business kind. Ceiling is
    # intentionally absent: a ceiling *detail* is a detail view, whereas a
    # ceiling plan/RCP is handled by the rule above.
    if "detail" in scores:
        specific_patterns = {
            "door": r"门表|门大样|门详图|DOOR\s+(?:SCHEDULE|DETAIL)",
            "floor": r"地花|地面铺装|地坪|FLOOR\s+(?:FINISH|PATTERN)",
        }
        for kind, pattern in specific_patterns.items():
            if kind not in scores:
                continue
            for _source_name, text, source_weight in sources:
                if re.search(pattern, text, re.IGNORECASE) and re.search(
                    r"节点|大样|详图|剖面|DETAIL|SECTION", text, re.IGNORECASE
                ):
                    scores["detail"] = max(0.0, scores["detail"] - 4.2 * source_weight)

    ordered = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
    floor_number_titles = [
        title
        for title in titles
        if re.search(
            r"(?<![A-Z0-9])(?:B\d+|\d+F|L\d+)-[A-Z0-9]{1,4}-\d{1,3}(?!\d)",
            title,
            re.IGNORECASE,
        )
    ]
    number_titles = [
        title
        for title in titles
        if title not in floor_number_titles
        if re.search(
            r"图号|DRAWING\s*(?:NO\.?|NUMBER)|立面|平面|节点|大样|详图|"
            r"ELEVATION|PLAN|DETAIL|SECTION",
            title,
            re.IGNORECASE,
        )
    ]
    inferred_number = drawing_number or extract_drawing_number(
        [file_text, layout_name or "", *floor_number_titles, *number_titles]
    )
    title = _candidate_title(primary_titles or titles, path.stem)
    if not ordered:
        return ClassificationResult(
            kind="unknown",
            confidence=0.0,
            drawing_number=inferred_number,
            title=title,
        )

    top_kind, top_score = ordered[0]
    second_score = ordered[1][1] if len(ordered) > 1 else 0.0
    margin = top_score - second_score
    evidence = evidence_by_kind.get(top_kind, [])

    # A code prefix alone or a genuine tie is not enough for automatic classification.
    if top_score < 3.0 or (second_score >= 3.0 and margin < 1.0):
        all_evidence = [entry for values in evidence_by_kind.values() for entry in values]
        return ClassificationResult(
            kind="unknown",
            confidence=round(min(0.49, 0.18 + top_score / 20), 3),
            drawing_number=inferred_number,
            title=title,
            evidence=all_evidence[:12],
            scores={key: round(value, 3) for key, value in ordered},
        )

    confidence = 0.48 + 0.3 * (1 - math.exp(-top_score / 5))
    confidence += min(0.16, margin / 25)
    return ClassificationResult(
        kind=top_kind,
        confidence=round(min(0.96, confidence), 3),
        drawing_number=inferred_number,
        title=title,
        evidence=evidence[:12],
        scores={key: round(value, 3) for key, value in ordered},
    )
