# Structured data schema

The canonical Pydantic definitions live in `scripts/cadquote/models.py`. JSON is the current stage-exchange format; SQLite is the query/audit mirror and uses the same identities and field meanings.

## Identity and traceability

- `SourceFile.id`: `file:<content-sha256>:<relative-path-sha256-prefix>`；内容哈希仍单独保存在 `sha256` 字段。
- `Sheet.id`: stable hash of source file id + layout/viewport/drawing number.
- `CadEntity.id`: stable hash of source file id + space + handle; use a geometry hash when a handle is absent.
- `MtOccurrence.id`, `ComponentInstance.id`, and `EvidenceEdge.id` must be deterministic for identical inputs.
- Keep source-relative paths in outputs; absolute paths may appear only in local run diagnostics.

## PASS requirements

A `TakeoffItem` may be PASS only when:

1. its MT code resolves to a non-conflicting material record;
2. its physical component instance is not a duplicate;
3. plan location, elevation and detail evidence are linked, unless a documented pricing rule explicitly makes a stage unnecessary;
4. width/length/quantity are backed by entity handles or an approved visual/manual override;
5. the deterministic calculation validates;
6. an approved, versioned, exact price entry matches the material and pricing context.

Otherwise the item remains REVIEW or BLOCK with a reason. A run without a price book may contain a valid quantity draft, but it is not a commercial PASS quotation.

## Quote workbook fields

The required business columns are:

`序号、名称、MT编号、材料、平面图位置、对应立面、对应节点、展开规格、宽、长度、数量、工程量、单位、计价方式、单价、金额、备注`.

Additional sheets store entity-level evidence, unresolved issues, price provenance, and run metadata.
