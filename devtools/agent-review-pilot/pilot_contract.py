"""Local CAD-only experiment bookkeeping; not a sandbox or an accuracy evaluator.

Only the Python standard library is used. Runtime manifests and frozen results
can contain private paths and must stay outside the public repository.
"""

from __future__ import annotations

import argparse
import ast
import copy
import hashlib
import json
import math
import re
import struct
import zlib
from pathlib import Path


class ContractError(ValueError):
    """An experiment record is structurally unsafe, stale, or inconsistent."""


SCHEMA = "agent-review-pilot/1"
FIELDS = {
    "name",
    "mt_code",
    "material",
    "plan_location",
    "elevation",
    "detail",
    "specification",
    "width_mm",
    "length_mm",
    "quantity",
    "engineering_quantity",
    "unit",
    "calc_kind",
}
NUMERIC = {"width_mm", "length_mm", "quantity", "engineering_quantity"}
QUANTITY_ROLES = {
    "physical_instance_count",
    "paired_jamb_count",
    "billable_face_count",
    "surround_edge_count",
}
LIMITATIONS = {
    "verification_scope": "declared_file_byte_audit_not_os_isolation",
    "code_may_embed_answers": True,
    "policy_text_may_embed_prior_knowledge": True,
    "access_declarations_are_self_reported": True,
    "code_inventory_is_caller_declared": True,
    "executed_code_and_dependencies_are_not_proven": True,
    "environment_is_not_frozen": True,
    "cad_signature_is_not_full_cad_validation": True,
    "cad_entity_ownership_is_not_verified": True,
    "formula_dimensional_semantics_are_not_verified": True,
    "image_bytes_do_not_prove_cad_render_provenance": True,
    "blindness_guaranteed": False,
    "accuracy_evaluated": False,
    "commercial_pass": False,
}


def require(ok, message):
    if not ok:
        raise ContractError(message)


def keys(obj, required, optional=(), where="object"):
    require(isinstance(obj, dict), f"{where}: expected object")
    require(set(required) <= obj.keys(), f"{where}: missing keys {set(required) - obj.keys()}")
    require(
        obj.keys() <= set(required) | set(optional),
        f"{where}: unexpected keys {obj.keys() - set(required) - set(optional)}",
    )


def text(value, where):
    require(isinstance(value, str) and bool(value.strip()), f"{where}: expected non-empty string")
    return value


def finite_number(value):
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def number(value, where, positive=False):
    require(finite_number(value), f"{where}: expected finite number, not boolean")
    require(value > 0 if positive else value >= 0, f"{where}: invalid sign")
    return value


def choice(value, allowed, where):
    require(isinstance(value, str) and value in allowed, f"{where}: invalid choice")


def references(value, allowed, where):
    require(
        isinstance(value, list) and value and all(isinstance(x, str) and x for x in value),
        f"{where}: non-empty string list required",
    )
    require(
        len(value) == len(set(value)) and set(value) <= set(allowed),
        f"{where}: duplicate, missing, or cross-component reference",
    )


def canonical(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def digest(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def file_hash(path):
    with Path(path).open("rb") as stream:
        h = hashlib.sha256()
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def load_json(path):
    def no_duplicates(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result, f"duplicate JSON key: {key}")
            result[key] = value
        return result

    try:
        return json.loads(
            Path(path).read_text("utf-8-sig"),
            object_pairs_hook=no_duplicates,
            parse_constant=lambda v: (_ for _ in ()).throw(ContractError(f"nonfinite JSON: {v}")),
        )
    except (OSError, UnicodeError, ValueError) as exc:
        raise ContractError(f"invalid JSON file {path}: {exc}") from exc


def file_record(record, base):
    keys(record, {"path", "sha256"}, where="file reference")
    p = Path(text(record["path"], "file.path"))
    p = (Path(base) / p).resolve() if not p.is_absolute() else p.resolve()
    require(p.is_file(), f"missing file: {p}")
    h = text(record["sha256"], "file.sha256")
    require(re.fullmatch(r"[0-9a-f]{64}", h) is not None, "invalid SHA-256")
    require(file_hash(p) == h, f"file hash drift: {p}")
    return {"path": str(p), "sha256": h}


def cad_signature(path):
    p = Path(path)
    require(
        p.suffix.lower() in {".dwg", ".dxf"},
        "source must be an explicit DWG or DXF, not an archive/workbook/image",
    )
    with p.open("rb") as stream:
        head = stream.read(8192)
    if p.suffix.lower() == ".dwg":
        require(re.match(rb"AC10[0-9]{2}", head) is not None, "DWG signature mismatch")
    else:
        binary = head.startswith(b"AutoCAD Binary DXF\r\n\x1a\x00")
        ascii_dxf = re.search(rb"(?:^|\n)\s*0\s*\r?\n\s*SECTION\s*\r?\n", head) is not None
        require(binary or ascii_dxf, "DXF signature mismatch")


def policy_schema(policy):
    keys(policy, {"schema", "policy_id", "version", "scope", "rules"}, {"approval"}, "policy")
    require(policy["schema"] == "pilot-rules/1", "unsupported policy schema")
    text(policy["policy_id"], "policy_id")
    text(policy["version"], "policy version")
    choice(policy["scope"], {"general", "confirmed_policy"}, "policy scope")
    if policy["scope"] == "confirmed_policy":
        keys(policy.get("approval"), {"by", "at", "basis"}, where="policy approval")
        for k, v in policy["approval"].items():
            text(v, f"approval.{k}")
    else:
        require("approval" not in policy, "general policy cannot include approval")
    require(isinstance(policy["rules"], list) and policy["rules"], "rules must be non-empty")
    ids = []
    for rule in policy["rules"]:
        keys(rule, {"rule_id", "statement"}, where="rule (selectors/targets are not allowed)")
        ids.append(text(rule["rule_id"], "rule_id"))
        text(rule["statement"], "rule statement")
    require(len(ids) == len(set(ids)), "duplicate rule IDs")


def validate_manifest(manifest, base_dir="."):
    keys(
        manifest,
        {"schema", "experiment_id", "sources", "policy_files"},
        where="input manifest (no selectors)",
    )
    require(manifest["schema"] == SCHEMA, "unsupported manifest schema")
    text(manifest["experiment_id"], "experiment_id")
    require(
        isinstance(manifest["sources"], list) and manifest["sources"],
        "non-empty CAD source list required",
    )
    require(isinstance(manifest["policy_files"], list), "policy_files must be a list")
    sources = []
    policies = []
    for s in manifest["sources"]:
        keys(s, {"source_id", "path", "sha256"}, where="CAD source (no bbox/handle/row selectors)")
        text(s["source_id"], "source_id")
        record = file_record({k: s[k] for k in ("path", "sha256")}, base_dir)
        cad_signature(record["path"])
        sources.append({"source_id": s["source_id"], **record})
    require(len({s["source_id"] for s in sources}) == len(sources), "duplicate source IDs")
    require(len({s["path"] for s in sources}) == len(sources), "duplicate source paths")
    for p in manifest["policy_files"]:
        record = file_record(p, base_dir)
        policy_schema(load_json(record["path"]))
        policies.append(record)
    require(len({p["path"] for p in policies}) == len(policies), "duplicate policy files")
    require(
        not ({s["path"] for s in sources} & {p["path"] for p in policies}),
        "source cannot also be policy",
    )
    return {
        "schema": SCHEMA,
        "experiment_id": manifest["experiment_id"],
        "sources": sorted(sources, key=lambda s: s["source_id"]),
        "policy_files": sorted(policies, key=lambda p: p["path"]),
    }


def png_size(path):
    """Chunk structure + CRC + bounded zlib/scanlines for non-interlaced 8-bit PNG."""
    require(Path(path).stat().st_size <= 128 * 1024 * 1024, "PNG exceeds audit size cap")
    data = Path(path).read_bytes()
    require(
        data.startswith(b"\x89PNG\r\n\x1a\n"), "evidence must be a real PNG (not just a .png name)"
    )
    offset = 8
    header = None
    compressed = []
    idat_ended = False
    palette_seen = False
    transparency_seen = False
    ended = False
    while offset < len(data):
        require(offset + 12 <= len(data), "truncated PNG chunk")
        size = struct.unpack(">I", data[offset : offset + 4])[0]
        kind = data[offset + 4 : offset + 8]
        require(
            re.fullmatch(rb"[A-Za-z]{4}", kind) is not None and not kind[2] & 0x20,
            "invalid PNG chunk type/reserved bit",
        )
        require(
            kind[0] & 0x20 or kind in {b"IHDR", b"PLTE", b"IDAT", b"IEND"},
            "unsupported PNG critical chunk",
        )
        payload = data[offset + 8 : offset + 8 + size]
        end = offset + 12 + size
        require(end <= len(data), "truncated PNG payload")
        crc = struct.unpack(">I", data[end - 4 : end])[0]
        require(zlib.crc32(kind + payload) & 0xFFFFFFFF == crc, "PNG CRC mismatch")
        if header is None:
            require(kind == b"IHDR" and size == 13, "PNG must begin with IHDR")
        if kind == b"IHDR":
            require(header is None, "duplicate PNG IHDR")
            header = struct.unpack(">IIBBBBB", payload)
        elif kind == b"PLTE":
            require(
                not palette_seen
                and not compressed
                and not transparency_seen
                and header[3] in {2, 6}
                and 0 < size <= 768
                and size % 3 == 0,
                "invalid PNG palette/order",
            )
            palette_seen = True
        elif kind == b"IDAT":
            require(not idat_ended, "PNG IDAT chunks must be consecutive")
            compressed.append(payload)
        elif kind == b"tRNS":
            require(
                not transparency_seen
                and not compressed
                and ((header[3] == 0 and size == 2) or (header[3] == 2 and size == 6)),
                "invalid PNG transparency/order",
            )
            require(
                all(sample <= 255 for sample in struct.unpack(f">{size // 2}H", payload)),
                "PNG transparency exceeds 8-bit sample range",
            )
            transparency_seen = True
        elif kind == b"IEND":
            require(size == 0 and end == len(data), "invalid PNG end/trailing data")
            ended = True
            break
        if compressed and kind != b"IDAT":
            idat_ended = True
        offset = end
    require(ended and header is not None and compressed, "incomplete PNG")
    w, h, depth, color, compression, filter_method, interlace = header
    require(w > 0 and h > 0 and w * h <= 40_000_000, "invalid/oversized PNG dimensions")
    require(
        depth == 8 and color in {0, 2, 4, 6} and compression == filter_method == interlace == 0,
        "PNG audit supports only non-interlaced 8-bit gray/RGB/gray-alpha/RGBA",
    )
    channels = {0: 1, 2: 3, 4: 2, 6: 4}[color]
    stride = w * channels + 1
    expected = stride * h
    try:
        decoder = zlib.decompressobj()
        pixels = decoder.decompress(b"".join(compressed), expected + 1)
    except zlib.error as exc:
        raise ContractError("invalid PNG compression") from exc
    require(
        len(pixels) == expected
        and decoder.eof
        and not decoder.unused_data
        and not decoder.unconsumed_tail,
        "PNG decoded size mismatch",
    )
    require(all(pixels[i] <= 4 for i in range(0, expected, stride)), "invalid PNG scanline filter")
    return [w, h]


def formula_value(expression, bindings):
    require(isinstance(expression, str) and 0 < len(expression) <= 4096, "invalid formula")
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        raise ContractError("formula syntax") from exc
    require(sum(1 for _ in ast.walk(tree)) <= 512, "formula too complex")
    used = set()

    def walk(n):
        if isinstance(n, ast.Expression):
            return walk(n.body)
        if isinstance(n, ast.Name):
            require(n.id in bindings, f"unbound formula term {n.id}")
            used.add(n.id)
            return bindings[n.id]
        if isinstance(n, ast.Constant):
            return number(n.value, "formula constant")
        if isinstance(n, ast.BinOp) and isinstance(n.op, (ast.Add, ast.Sub, ast.Mult, ast.Div)):
            a, b = walk(n.left), walk(n.right)
            try:
                if isinstance(n.op, ast.Add):
                    value = a + b
                elif isinstance(n.op, ast.Sub):
                    value = a - b
                elif isinstance(n.op, ast.Mult):
                    value = a * b
                else:
                    require(b != 0, "formula division by zero")
                    value = a / b
            except OverflowError as exc:
                raise ContractError("formula arithmetic overflow") from exc
            require(finite_number(value), "formula non-finite/out-of-range result")
            return value
        raise ContractError("unsupported/unsafe formula node")

    result = walk(tree)
    require(
        bool(used) and used == set(bindings), "formula must use all non-empty evidence-bound terms"
    )
    return result


def validate_predictions(prediction, manifest, base_dir="."):
    keys(
        prediction, {"schema", "declared_access", "source_audit", "components"}, where="prediction"
    )
    require(prediction["schema"] == SCHEMA, "unsupported prediction schema")
    access = prediction["declared_access"]
    keys(
        access,
        {
            "numeric_gold_accessed",
            "human_screenshots_accessed",
            "old_predictions_accessed",
            "prior_labels_in_context",
        },
        where="declared_access",
    )
    require(all(type(v) is bool for v in access.values()), "access declarations must be boolean")
    require(
        not any(access[k] for k in access if k != "prior_labels_in_context"),
        "declared non-CAD answer access prevents target-free freeze",
    )
    sources = {s["source_id"]: s for s in manifest["sources"]}
    audits = prediction["source_audit"]
    require(isinstance(audits, list), "source_audit must be list")
    for a in audits:
        keys(a, {"source_id", "state", "reason"}, where="source audit")
        text(a["source_id"], "source audit source_id")
        choice(a["state"], {"SCANNED", "UNSUPPORTED", "ERROR"}, "source audit state")
        require(isinstance(a["reason"], str), "source reason must be text")
        if a["state"] != "SCANNED":
            text(a["reason"], "non-scanned source reason")
    require(
        len(audits) == len(sources) and {a["source_id"] for a in audits} == set(sources),
        "missing/extra/duplicate source audit",
    )
    result = copy.deepcopy(prediction)
    components = result["components"]
    require(isinstance(components, list), "components must be list")
    ids = []
    global_instances = []
    global_evidence = []
    for c in components:
        keys(
            c,
            {
                "component_id",
                "fields",
                "quantity_role",
                "instances",
                "formula",
                "evidence",
                "stage_states",
                "state",
                "unknowns",
            },
            where="component",
        )
        cid = text(c["component_id"], "component_id")
        ids.append(cid)
        choice(c["state"], {"PREDICTED", "REVIEW", "BLOCK"}, "prediction-only component state")
        require(
            isinstance(c["unknowns"], list)
            and all(isinstance(x, str) and x for x in c["unknowns"]),
            "unknowns must be explicit text list",
        )
        keys(c["stage_states"], {"plan", "elevation", "detail"}, where="stage_states")
        for v in c["stage_states"].values():
            choice(v, {"CANDIDATE", "MISSING", "NOT_APPLICABLE_CLAIMED"}, "stage state")
        if any(v != "CANDIDATE" for v in c["stage_states"].values()):
            require(
                c["state"] != "PREDICTED" and bool(c["unknowns"]),
                "missing/inapplicable stage needs explicit review reason",
            )
        keys(c["fields"], FIELDS, where="component fields")
        for k, v in c["fields"].items():
            if v is None:
                continue
            if k in NUMERIC:
                number(v, k, positive=True)
            else:
                text(v, k)
        q = c["fields"]["quantity"]
        if c["quantity_role"] is not None:
            choice(c["quantity_role"], QUANTITY_ROLES, "quantity role")
        require(
            q is None or (int(q) == q and c["quantity_role"] is not None),
            "count requires integer and typed quantity role",
        )
        require(
            isinstance(c["instances"], list) and isinstance(c["evidence"], list),
            "instances/evidence must be lists",
        )
        eids = []
        for e in c["evidence"]:
            keys(
                e,
                {
                    "evidence_id",
                    "source_id",
                    "layout",
                    "stage",
                    "role",
                    "state",
                    "handles",
                    "bbox",
                    "image",
                },
                where="evidence",
            )
            eid = text(e["evidence_id"], "evidence_id")
            eids.append(eid)
            global_evidence.append(eid)
            choice(e["source_id"], sources, "evidence source")
            text(e["layout"], "evidence layout")
            choice(e["stage"], {"plan", "elevation", "detail", "material"}, "evidence stage")
            choice(
                e["role"],
                {"locator", "closeup", "dimension", "geometry", "material", "reference"},
                "evidence role",
            )
            choice(e["state"], {"CANDIDATE", "REVIEW", "MISSING"}, "unconfirmed evidence state")
            require(
                isinstance(e["handles"], list)
                and all(isinstance(h, str) and h for h in e["handles"]),
                "handles must be runtime string list",
            )
            require(len(e["handles"]) == len(set(e["handles"])), "duplicate handles in evidence")
            b = e["bbox"]
            if b is not None:
                require(
                    isinstance(b, list) and len(b) == 4 and all(finite_number(v) for v in b),
                    "invalid bbox",
                )
                require(b[0] < b[2] and b[1] < b[3], "degenerate bbox")
            if e["image"] is not None:
                keys(e["image"], {"path", "sha256", "pixels"}, where="evidence image")
                im = file_record({k: e["image"][k] for k in ("path", "sha256")}, base_dir)
                size = png_size(im["path"])
                require(
                    isinstance(e["image"]["pixels"], list)
                    and all(type(v) is int for v in e["image"]["pixels"])
                    and e["image"]["pixels"] == size,
                    "PNG natural pixel mismatch",
                )
                e["image"] = {**im, "pixels": size}
            if e["state"] == "MISSING":
                require(e["image"] is None, "MISSING evidence cannot claim an image")
            if e["role"] in {"locator", "closeup"} and e["image"] is None:
                require(
                    e["state"] == "MISSING", "missing locator/closeup must be explicitly MISSING"
                )
        for stage, state in c["stage_states"].items():
            if state == "CANDIDATE":
                require(
                    any(e["stage"] == stage and e["state"] != "MISSING" for e in c["evidence"]),
                    f"{stage} candidate stage has no evidence",
                )
            elif state == "MISSING":
                require(
                    not any(e["stage"] == stage and e["state"] != "MISSING" for e in c["evidence"]),
                    f"{stage} is MISSING but evidence claims available",
                )
        for instance in c["instances"]:
            keys(instance, {"instance_id", "evidence_ids"}, where="physical instance")
            global_instances.append(text(instance["instance_id"], "instance_id"))
            references(instance["evidence_ids"], eids, "instance evidence")
        if q is not None and c["quantity_role"] == "physical_instance_count":
            require(
                q == len(c["instances"]), "physical quantity differs from explicit instance count"
            )
        f = c["formula"]
        if f is not None:
            keys(f, {"expression", "terms", "result", "output_unit"}, where="formula")
            require(isinstance(f["terms"], list) and f["terms"], "formula needs terms")
            bindings = {}
            for term in f["terms"]:
                keys(
                    term, {"symbol", "value", "unit", "role", "evidence_ids"}, where="formula term"
                )
                symbol = text(term["symbol"], "term symbol")
                require(
                    symbol.isidentifier() and symbol not in bindings,
                    "invalid/duplicate formula symbol",
                )
                bindings[symbol] = number(term["value"], "term value", positive=True)
                choice(term["unit"], {"mm", "m", "m2", "count", "ratio"}, "term unit")
                text(term["role"], "measurement role")
                references(term["evidence_ids"], eids, "formula term evidence")
                require(
                    (term["unit"] == "count") == (term["role"] in QUANTITY_ROLES),
                    "count units and typed quantity roles must agree",
                )
                if term["unit"] == "count":
                    require(
                        term["role"] in QUANTITY_ROLES and int(term["value"]) == term["value"],
                        "count term needs integer and typed role",
                    )
                    if term["role"] == "physical_instance_count":
                        require(
                            term["value"] == len(c["instances"]),
                            "formula physical count differs from instance inventory",
                        )
                    if q is not None and term["role"] == c["quantity_role"]:
                        require(
                            term["value"] == q,
                            "formula count differs from same-role visible quantity",
                        )
            value = formula_value(f["expression"], bindings)
            require(
                math.isclose(
                    number(f["result"], "formula result", positive=True),
                    value,
                    rel_tol=1e-12,
                    abs_tol=1e-12,
                ),
                "formula result mismatch",
            )
            text(f["output_unit"], "formula output unit")
        eq = c["fields"]["engineering_quantity"]
        if eq is not None:
            require(
                f is not None and math.isclose(eq, f["result"], rel_tol=1e-12, abs_tol=1e-12),
                "engineering quantity needs matching explicit formula",
            )
            require(c["fields"]["unit"] == f["output_unit"], "engineering and formula units differ")
        if any(v is None for v in c["fields"].values()):
            require(
                c["state"] != "PREDICTED" and bool(c["unknowns"]),
                "unknown fields need REVIEW/BLOCK with reasons",
            )
    require(len(ids) == len(set(ids)), "duplicate component IDs")
    require(
        len(global_instances) == len(set(global_instances)),
        "duplicate physical instance IDs across components",
    )
    require(
        len(global_evidence) == len(set(global_evidence)),
        "duplicate evidence IDs across components",
    )
    return result


def normalized_predictions(prediction):
    p = copy.deepcopy(prediction)
    p["source_audit"].sort(key=lambda x: x["source_id"])
    p["components"].sort(key=lambda x: x["component_id"])
    for c in p["components"]:
        c["instances"].sort(key=lambda x: x["instance_id"])
        c["evidence"].sort(key=lambda x: x["evidence_id"])
        for i in c["instances"]:
            i["evidence_ids"].sort()
        for e in c["evidence"]:
            e["handles"].sort()
            if e["image"]:
                e["image"].pop("path")  # fresh output folders are not semantic drift
        if c["formula"]:
            c["formula"]["terms"].sort(key=lambda x: x["symbol"])
            for t in c["formula"]["terms"]:
                t["evidence_ids"].sort()
    return p


def freeze(manifest, prediction, code_files, base_dir="."):
    inputs = validate_manifest(manifest, base_dir)
    require(isinstance(code_files, list) and code_files, "explicit code file list required")
    code = []
    for value in code_files + [str(Path(__file__).resolve())]:
        path = Path(value)
        path = (Path(base_dir) / path).resolve() if not path.is_absolute() else path.resolve()
        require(path.is_file(), f"missing code file {path}")
        if str(path) not in {c["path"] for c in code}:
            code.append({"path": str(path), "sha256": file_hash(path)})
    pred = validate_predictions(prediction, inputs, base_dir)
    normalized = normalized_predictions(pred)
    body = {
        "schema": SCHEMA,
        "status": "PREDICTION_FROZEN" if pred["components"] else "EMPTY",
        "manifest": inputs,
        "input_hash": digest(inputs),
        "code_files": sorted(code, key=lambda c: c["path"]),
        "predictions": pred,
        "prediction_hash": digest(normalized),
        "limitations": copy.deepcopy(LIMITATIONS),
    }
    body["freeze_hash"] = digest(body)
    verify_frozen(body)
    return body


def verify_frozen(frozen):
    keys(
        frozen,
        {
            "schema",
            "status",
            "manifest",
            "input_hash",
            "code_files",
            "predictions",
            "prediction_hash",
            "limitations",
            "freeze_hash",
        },
        where="frozen result",
    )
    require(frozen["schema"] == SCHEMA, "unsupported frozen schema")
    body = {k: v for k, v in frozen.items() if k != "freeze_hash"}
    require(digest(body) == frozen["freeze_hash"], "frozen record hash mismatch")
    require(frozen["limitations"] == LIMITATIONS, "freeze must retain explicit limitations")
    manifest = validate_manifest(frozen["manifest"])
    require(digest(manifest) == frozen["input_hash"], "input manifest fingerprint mismatch")
    require(frozen["code_files"], "frozen code inventory missing")
    for c in frozen["code_files"]:
        file_record(c, ".")
    pred = validate_predictions(frozen["predictions"], manifest)
    require(
        digest(normalized_predictions(pred)) == frozen["prediction_hash"],
        "prediction fingerprint mismatch",
    )
    require(
        frozen["status"] == ("PREDICTION_FROZEN" if pred["components"] else "EMPTY"),
        "invalid frozen status",
    )
    return True


def compare(first, second):
    verify_frozen(first)
    verify_frozen(second)
    a = normalized_predictions(first["predictions"])
    b = normalized_predictions(second["predictions"])
    aa = {c["component_id"]: c for c in a["components"]}
    bb = {c["component_id"]: c for c in b["components"]}
    missing = sorted(aa.keys() - bb.keys())
    extra = sorted(bb.keys() - aa.keys())
    changed = {
        k: sorted(f for f in aa[k] if canonical(aa[k][f]) != canonical(bb[k][f]))
        for k in aa.keys() & bb.keys()
        if canonical(aa[k]) != canonical(bb[k])
    }
    context_equal = (
        first["input_hash"] == second["input_hash"] and first["code_files"] == second["code_files"]
    )
    noncomponent_equal = {k: v for k, v in a.items() if k != "components"} == {
        k: v for k, v in b.items() if k != "components"
    }
    same = not missing and not extra and not changed and noncomponent_equal
    status = (
        "CONTEXT_DRIFT"
        if not context_equal
        else ("EMPTY" if not aa and not bb else ("REPEATED" if same else "DIFFERENT"))
    )
    return {
        "schema": SCHEMA,
        "status": status,
        "repeatable_nonempty": status == "REPEATED",
        "missing_in_second": missing,
        "extra_in_second": extra,
        "changed_components": changed,
        "noncomponent_state_equal": noncomponent_equal,
        "context_equal": context_equal,
        "normalized_equal": same,
        "accuracy_evaluated": False,
        "blindness_guaranteed": False,
        "commercial_pass": False,
    }


def write_new_json(path, payload):
    """Never overwrite an input, an older freeze, or another artifact."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("freeze")
    p.add_argument("manifest")
    p.add_argument("prediction")
    p.add_argument("--code", action="append", required=True)
    p.add_argument("--out", required=True)
    p = sub.add_parser("verify")
    p.add_argument("frozen")
    p = sub.add_parser("compare")
    p.add_argument("first")
    p.add_argument("second")
    p.add_argument("--out", required=True)
    args = parser.parse_args()
    try:
        if args.command == "freeze":
            value = freeze(
                load_json(args.manifest),
                load_json(args.prediction),
                args.code,
                Path(args.manifest).resolve().parent,
            )
            write_new_json(args.out, value)
        elif args.command == "verify":
            value = load_json(args.frozen)
            verify_frozen(value)
        else:
            value = compare(load_json(args.first), load_json(args.second))
            write_new_json(args.out, value)
        print(
            json.dumps({"status": value["status"], "accuracy_evaluated": False}, ensure_ascii=False)
        )
        return 2 if value["status"] in {"EMPTY", "DIFFERENT", "CONTEXT_DRIFT"} else 0
    except (ContractError, OSError) as exc:
        print(json.dumps({"status": "INVALID", "error": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
