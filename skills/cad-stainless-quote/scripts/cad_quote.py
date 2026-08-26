#!/usr/bin/env python3
"""CLI for traceable stainless-steel CAD takeoff and quotation."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from cadquote.cad_index import build_cad_index
from cadquote.converter import convert_dwgs
from cadquote.doctor import run_doctor
from cadquote.evaluation import evaluate_takeoff
from cadquote.exporter import build_quote_workbook
from cadquote.gold import import_gold_workbook
from cadquote.ingest import ingest_input
from cadquote.io import write_json_atomic
from cadquote.linking import rank_evidence_edges
from cadquote.materials import load_material_specs
from cadquote.models import (
    CadEntity,
    EvidenceEdge,
    MaterialSpec,
    MeasurementCandidate,
    MtOccurrence,
    RunIssue,
    Severity,
    Sheet,
    TakeoffItem,
)
from cadquote.mt import detect_mt_occurrences
from cadquote.panels import PanelExpansion, choose_analysis_view
from cadquote.pipeline import load_confirmation_bundle, resume_pipeline, run_pipeline
from cadquote.render import load_regions_json, render_regions, viewport_model_regions
from cadquote.takeoff import build_takeoff


def _print(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def _load_json(path: Path | str) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _pipeline_cli_summary(result: Any) -> dict[str, Any]:
    severity_counts = Counter(issue.severity.value for issue in result.issues)
    code_counts = Counter(issue.code for issue in result.issues)
    return {
        "status": result.status.value,
        "safe_outcome": (
            "BLOCK means unresolved evidence was withheld; inspect review_pack and issues"
            if result.status.value == "BLOCK"
            else None
        ),
        "run_dir": result.run_dir,
        "quote_path": result.quote_path,
        "manifest_path": result.manifest_path,
        "counts": result.counts,
        "issue_count": len(result.issues),
        "issue_severity_counts": dict(sorted(severity_counts.items())),
        "top_issue_codes": dict(code_counts.most_common(12)),
        "paths": {
            key: value
            for key, value in result.paths.items()
            if key in {"review_pack", "issues", "takeoff", "evidence_graph"}
        },
    }


def _parse_bool(value: str) -> bool:
    normalized = value.strip().casefold()
    if normalized in {"true", "1", "yes", "y", "是"}:
        return True
    if normalized in {"false", "0", "no", "n", "否"}:
        return False
    raise argparse.ArgumentTypeError("must be true or false")


def _takeoff_rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        raise ValueError("takeoff JSON must be an array or object")
    if isinstance(payload.get("items"), list):
        return payload["items"]
    if isinstance(payload.get("rows"), list):
        return [
            value["item"] if isinstance(value, dict) and "item" in value else value
            for value in payload["rows"]
        ]
    raise ValueError("takeoff JSON has no items or gold rows")


def _load_index(path: Path | str) -> tuple[list[Sheet], list[CadEntity]]:
    payload = _load_json(path)
    sources = payload.get("sources", []) if isinstance(payload, dict) else []
    sheets = [
        Sheet.model_validate(value)
        for source in sources
        for value in source.get("sheets", [])
    ]
    entities = [
        CadEntity.model_validate(value)
        for source in sources
        for value in source.get("entities", [])
    ]
    return sheets, entities


def _apply_panels(
    sheets: list[Sheet],
    entities: list[CadEntity],
    panels_path: Path | str | None,
) -> tuple[list[Sheet], list[CadEntity]]:
    if panels_path is None:
        return sheets, entities
    panel_payload = _load_json(panels_path)
    expansion = PanelExpansion(
        sheets=[Sheet.model_validate(value) for value in panel_payload.get("sheets", [])],
        entities=[
            CadEntity.model_validate(value) for value in panel_payload.get("entities", [])
        ],
        source_panel_counts={
            str(key): int(value)
            for key, value in panel_payload.get("source_panel_counts", {}).items()
        },
        warnings=[str(value) for value in panel_payload.get("warnings", [])],
    )
    return choose_analysis_view(sheets, entities, expansion)


def command_doctor(_: argparse.Namespace) -> int:
    report = run_doctor()
    _print(report)
    return 0 if report["status"] == "PASS" else 2


def command_run(args: argparse.Namespace) -> int:
    result = run_pipeline(
        args.input,
        args.out,
        price_book=args.price_book,
        confirmations=args.confirmations,
        quote_date=args.quote_date,
        currency=args.currency,
        tax_included=args.tax_included,
        render_evidence=not args.no_render,
    )
    _print(_pipeline_cli_summary(result))
    return 0 if result.status.value in {"PASS", "REVIEW"} else 2


def command_resume(args: argparse.Namespace) -> int:
    result = resume_pipeline(
        args.run_dir,
        price_book=args.price_book,
        confirmations=args.confirmations,
        quote_date=args.quote_date,
        currency=args.currency,
        tax_included=args.tax_included,
        render_evidence=not args.no_render,
    )
    _print(_pipeline_cli_summary(result))
    return 0 if result.status.value in {"PASS", "REVIEW"} else 2


def command_ingest(args: argparse.Namespace) -> int:
    result = ingest_input(args.input, args.out)
    payload = result.model_dump(mode="json")
    write_json_atomic(Path(args.out) / "ingest.json", payload)
    _print(payload)
    return 0 if result.succeeded else 2


def command_convert(args: argparse.Namespace) -> int:
    audit = convert_dwgs(args.input, args.out)
    payload = audit.to_dict()
    write_json_atomic(Path(args.out) / "conversion.json", payload)
    _print(payload)
    return 0 if audit.failed_count == 0 else 2


def command_index(args: argparse.Namespace) -> int:
    output = Path(args.out)
    output.mkdir(parents=True, exist_ok=True)
    bundle = build_cad_index(
        args.dxf,
        sqlite_path=output / "cad_index.sqlite",
        json_path=output / "cad_index.json",
    )
    _print(
        {
            "source_count": len(bundle.results),
            "sheet_count": len(bundle.sheets),
            "entity_count": len(bundle.entities),
            "json": str(output / "cad_index.json"),
            "sqlite": str(output / "cad_index.sqlite"),
        }
    )
    return 0


def command_materials(args: argparse.Namespace) -> int:
    values = load_material_specs(args.workbook)
    payload = [value.model_dump(mode="json") for value in values]
    write_json_atomic(Path(args.out), payload)
    _print({"material_count": len(values), "output": str(Path(args.out).resolve())})
    return 0


def command_detect_mt(args: argparse.Namespace) -> int:
    sheets, entities = _load_index(args.index)
    _, entities = _apply_panels(sheets, entities, args.panels)
    materials = (
        [MaterialSpec.model_validate(value) for value in _load_json(args.materials)]
        if args.materials
        else []
    )
    values = detect_mt_occurrences(entities, materials=materials)
    payload = [value.model_dump(mode="json") for value in values]
    write_json_atomic(Path(args.out), payload)
    _print({"occurrence_count": len(values), "output": str(Path(args.out).resolve())})
    return 0


def command_link(args: argparse.Namespace) -> int:
    sheets, entities = _load_index(args.index)
    sheets, entities = _apply_panels(sheets, entities, args.panels)
    occurrences = [MtOccurrence.model_validate(value) for value in _load_json(args.occurrences)]
    values = rank_evidence_edges(
        sheets,
        occurrences,
        entities,
        promote_explicit=args.promote_explicit,
    )
    payload = [value.model_dump(mode="json") for value in values]
    write_json_atomic(Path(args.out), payload)
    _print({"edge_count": len(values), "output": str(Path(args.out).resolve())})
    return 0


def command_takeoff(args: argparse.Namespace) -> int:
    sheets, entities = _load_index(args.index)
    sheets, entities = _apply_panels(sheets, entities, args.panels)
    occurrences = [MtOccurrence.model_validate(value) for value in _load_json(args.occurrences)]
    edges = [EvidenceEdge.model_validate(value) for value in _load_json(args.edges)]
    materials = (
        [MaterialSpec.model_validate(value) for value in _load_json(args.materials)]
        if args.materials
        else []
    )
    confirmations = load_confirmation_bundle(args.confirmations).selections
    result = build_takeoff(
        sheets,
        entities,
        occurrences,
        edges,
        materials=materials,
        confirmations=confirmations,
    )
    write_json_atomic(Path(args.out), result.to_dict())
    _print(
        {
            "component_count": len(result.components),
            "item_count": len(result.items),
            "review_issue_count": len(result.issues),
            "output": str(Path(args.out).resolve()),
        }
    )
    return 0


def command_quote(args: argparse.Namespace) -> int:
    payload = _load_json(args.takeoff)
    rows = _takeoff_rows(payload)
    items = [TakeoffItem.model_validate(value) for value in rows]
    edges: list[EvidenceEdge] = []
    measurements: list[MeasurementCandidate] = []
    issues: list[RunIssue] = []
    metadata: dict[str, Any] = {}
    limitations: list[str] = []
    if isinstance(payload, dict):
        edges = [
            EvidenceEdge.model_validate(value)
            for value in payload.get("evidence_edges", payload.get("edges", []))
        ]
        measurements = [
            MeasurementCandidate.model_validate(value)
            for value in payload.get("measurements", [])
        ]
        issues = [RunIssue.model_validate(value) for value in payload.get("issues", [])]
        raw_metadata = payload.get("metadata", {})
        metadata = dict(raw_metadata) if isinstance(raw_metadata, dict) else {}
        missing_sections = [
            name
            for name in ("measurements", "issues")
            if name not in payload
        ]
        if "evidence_edges" not in payload and "edges" not in payload:
            missing_sections.insert(0, "evidence_edges")
        if missing_sections:
            limitations.append("输入对象缺少证据区段: " + ", ".join(missing_sections))
    else:
        limitations.append("输入是旧版纯行数组，无法恢复证据边、尺寸候选和阶段问题")
    if limitations:
        issues.append(
            RunIssue(
                stage="export",
                severity=Severity.WARNING,
                code="PARTIAL_QUOTE_EVIDENCE_UNAVAILABLE",
                message="；".join(limitations),
                suggested_action="改用takeoff_draft.json或完整run/resume命令导出",
            )
        )
        metadata["export_limitations"] = limitations
    output = build_quote_workbook(
        items,
        args.out,
        edges=edges,
        measurements=measurements,
        issues=issues,
        metadata=metadata,
    )
    _print(
        {
            "item_count": len(items),
            "edge_count": len(edges),
            "measurement_count": len(measurements),
            "issue_count": len(issues),
            "limitations": limitations,
            "output": str(output.resolve()),
        }
    )
    return 0


def command_render(args: argparse.Namespace) -> int:
    if args.regions:
        regions = load_regions_json(args.regions)
        layout = args.layout
    else:
        regions = viewport_model_regions(args.dxf, layout=args.layout)
        layout = "Model"
    result = render_regions(args.dxf, regions, args.out, layout=layout)
    _print(result)
    return 0


def command_evaluate(args: argparse.Namespace) -> int:
    predicted_payload = _load_json(args.predicted)
    gold_payload = _load_json(args.gold)
    predicted_rows = _takeoff_rows(predicted_payload)
    gold_rows = _takeoff_rows(gold_payload)
    report = evaluate_takeoff(
        [TakeoffItem.model_validate(value) for value in predicted_rows],
        [TakeoffItem.model_validate(value) for value in gold_rows],
    )
    if args.out:
        write_json_atomic(Path(args.out), report)
    # The detailed row-key lists can be very large on real projects. Keep the
    # terminal result readable while preserving the complete report on disk.
    summary = {
        key: value
        for key, value in report.items()
        if key not in {"missing_keys", "unexpected_keys", "row_results"}
    }
    if args.out:
        summary["output"] = str(Path(args.out).resolve())
    _print(summary)
    return 0


def command_gold_import(args: argparse.Namespace) -> int:
    result = import_gold_workbook(args.workbook)
    result.write_json(args.out)
    _print(
        {
            "row_count": result.summary.row_count,
            "review_count": result.summary.review_count,
            "block_count": result.summary.block_count,
            "issue_count": result.summary.audit_issue_count,
            "output": str(Path(args.out).resolve()),
        }
    )
    return 0 if result.summary.row_count > 0 else 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    doctor = subparsers.add_parser("doctor", help="检查Python、DWG转换器和归档后端")
    doctor.set_defaults(handler=command_doctor)

    run = subparsers.add_parser("run", help="执行安全解包到报价输出的完整流程")
    run.add_argument("input", type=Path)
    run.add_argument("--out", type=Path, required=True)
    run.add_argument("--price-book", type=Path)
    run.add_argument("--confirmations", type=Path)
    run.add_argument("--quote-date", help="报价基准日期（YYYY-MM-DD；默认当天）")
    run.add_argument("--currency", default="CNY", help="价格币种（默认CNY）")
    run.add_argument("--tax-included", type=_parse_bool, help="要求价格含税口径true/false")
    run.add_argument("--no-render", action="store_true", help="跳过MT局部证据图渲染")
    run.set_defaults(handler=command_run)

    resume = subparsers.add_parser("resume", help="复用已有索引和候选，重跑审核、计价和导出")
    resume.add_argument("run_dir", type=Path)
    resume.add_argument("--price-book", type=Path)
    resume.add_argument("--confirmations", type=Path)
    resume.add_argument("--quote-date", help="报价基准日期（YYYY-MM-DD；默认继承原运行）")
    resume.add_argument("--currency", help="价格币种（默认继承原运行）")
    resume.add_argument(
        "--tax-included",
        type=_parse_bool,
        help="要求价格含税口径true/false（默认继承原运行）",
    )
    resume.add_argument("--no-render", action="store_true", help="跳过MT局部证据图渲染")
    resume.set_defaults(handler=command_resume)

    ingest = subparsers.add_parser("ingest", help="安全复制或解包输入")
    ingest.add_argument("input", type=Path)
    ingest.add_argument("--out", type=Path, required=True)
    ingest.set_defaults(handler=command_ingest)

    convert = subparsers.add_parser("convert", help="批量转换DWG为DXF并审计")
    convert.add_argument("input", type=Path)
    convert.add_argument("--out", type=Path, required=True)
    convert.set_defaults(handler=command_convert)

    index = subparsers.add_parser("index", help="构建DXF JSON/SQLite语义索引")
    index.add_argument("dxf", type=Path, nargs="+")
    index.add_argument("--out", type=Path, required=True)
    index.set_defaults(handler=command_index)

    materials = subparsers.add_parser("materials", help="解析XLS/XLSX材料表")
    materials.add_argument("workbook", type=Path)
    materials.add_argument("--out", type=Path, required=True)
    materials.set_defaults(handler=command_materials)

    detect = subparsers.add_parser("detect-mt", help="从CAD索引检测MT标注")
    detect.add_argument("index", type=Path)
    detect.add_argument("--materials", type=Path)
    detect.add_argument("--panels", type=Path, help="使用视口面板并保留未覆盖Model实体")
    detect.add_argument("--out", type=Path, required=True)
    detect.set_defaults(handler=command_detect_mt)

    link = subparsers.add_parser("link", help="生成平面→立面→节点候选关系")
    link.add_argument("index", type=Path)
    link.add_argument("occurrences", type=Path)
    link.add_argument("--panels", type=Path)
    link.add_argument("--out", type=Path, required=True)
    link.add_argument("--promote-explicit", action="store_true")
    link.set_defaults(handler=command_link)

    takeoff = subparsers.add_parser("takeoff", help="组装构件并生成尺寸候选/算量草稿")
    takeoff.add_argument("index", type=Path)
    takeoff.add_argument("occurrences", type=Path)
    takeoff.add_argument("edges", type=Path)
    takeoff.add_argument("--panels", type=Path)
    takeoff.add_argument("--materials", type=Path)
    takeoff.add_argument("--confirmations", type=Path)
    takeoff.add_argument("--out", type=Path, required=True)
    takeoff.set_defaults(handler=command_takeoff)

    quote = subparsers.add_parser("quote", help="将takeoff JSON导出便携报价工作簿")
    quote.add_argument("takeoff", type=Path)
    quote.add_argument("--out", type=Path, required=True)
    quote.set_defaults(handler=command_quote)

    render = subparsers.add_parser("render", help="渲染DXF局部证据图")
    render.add_argument("dxf", type=Path)
    render.add_argument("--out", type=Path, required=True)
    render.add_argument("--layout", default="Model")
    source_group = render.add_mutually_exclusive_group(required=True)
    source_group.add_argument("--regions", type=Path)
    source_group.add_argument("--viewports", action="store_true")
    render.set_defaults(handler=command_render)

    evaluate = subparsers.add_parser("evaluate", help="比较预测算量与审核金标准")
    evaluate.add_argument("predicted", type=Path)
    evaluate.add_argument("gold", type=Path)
    evaluate.add_argument("--out", type=Path)
    evaluate.set_defaults(handler=command_evaluate)

    gold = subparsers.add_parser("gold-import", help="导入并审计人工算量清单金标准")
    gold.add_argument("workbook", type=Path)
    gold.add_argument("--out", type=Path, required=True)
    gold.set_defaults(handler=command_gold_import)
    return parser


def main(argv: list[str] | None = None) -> int:
    # Proxy-object diagnostics are retained in the structured CAD index issues;
    # avoid flooding an interactive terminal with thousands of duplicate lines.
    logging.getLogger("ezdxf").setLevel(logging.ERROR)
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        _print({"status": "BLOCK", "error": f"{type(exc).__name__}: {exc}"})
        return 2


if __name__ == "__main__":
    sys.exit(main())
