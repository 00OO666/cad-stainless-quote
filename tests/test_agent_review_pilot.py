"""Entirely synthetic contract tests: no customer fixtures or source data."""

import copy
import hashlib
import importlib.util
import json
import struct
import subprocess
import sys
import tempfile
import unittest
import zlib
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "devtools/agent-review-pilot/pilot_contract.py"
SPEC = importlib.util.spec_from_file_location("agent_review_pilot_contract", MODULE_PATH)
pilot = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(pilot)


def png_chunk(kind, payload):
    return (
        struct.pack(">I", len(payload))
        + kind
        + payload
        + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
    )


def png(width=3, height=2, color=6, before=(), between=None, after=()):
    channels = {0: 1, 2: 3, 4: 2, 6: 4}[color]
    compressed = zlib.compress((b"\0" + bytes([31, 71, 121, 255][:channels]) * width) * height)
    idat = png_chunk(b"IDAT", compressed)
    if between is not None:
        idat = (
            png_chunk(b"IDAT", compressed[:2])
            + b"".join(between)
            + png_chunk(b"IDAT", compressed[2:])
        )
    return (
        b"\x89PNG\r\n\x1a\n"
        + png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, color, 0, 0, 0))
        + b"".join(before)
        + idat
        + b"".join(after)
        + png_chunk(b"IEND", b"")
    )


class AgentReviewPilotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.cad = self.base / "synth.dxf"
        self.cad.write_bytes(b"0\nSECTION\n2\nHEADER\n0\nENDSEC\n0\nEOF\n")
        self.code = self.base / "extractor.py"
        self.code.write_text("# synthetic extractor\n", encoding="utf8")
        self.image = self.base / "render.png"
        self.image.write_bytes(png())
        self.policy = self.base / "rules.json"
        self.policy.write_text(
            json.dumps(
                {
                    "schema": "pilot-rules/1",
                    "policy_id": "generic-policy",
                    "version": "1",
                    "scope": "general",
                    "rules": [
                        {
                            "rule_id": "never-count-labels",
                            "statement": "Material labels are not physical instance counts.",
                        }
                    ],
                }
            ),
            encoding="utf8",
        )
        self.manifest = {
            "schema": pilot.SCHEMA,
            "experiment_id": "synthetic-development",
            "sources": [{"source_id": "source-a", **self.ref(self.cad)}],
            "policy_files": [self.ref(self.policy)],
        }
        self.prediction = {
            "schema": pilot.SCHEMA,
            "declared_access": {
                "numeric_gold_accessed": False,
                "human_screenshots_accessed": False,
                "old_predictions_accessed": False,
                "prior_labels_in_context": True,
            },
            "source_audit": [{"source_id": "source-a", "state": "SCANNED", "reason": ""}],
            "components": [self.component("object-a")],
        }

    def ref(self, p):
        return {"path": str(p), "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}

    def component(self, name):
        eid = name + "-elevation"
        return {
            "component_id": name,
            "fields": {
                "name": "Synthetic panel",
                "mt_code": "SYN-ALPHA",
                "material": "synthetic metal",
                "plan_location": None,
                "elevation": "E-SYN",
                "detail": None,
                "specification": "projected rectangle",
                "width_mm": 173,
                "length_mm": 421,
                "quantity": 1,
                "engineering_quantity": 173 * 421 / 1_000_000,
                "unit": "m2",
                "calc_kind": "projected_area",
            },
            "quantity_role": "physical_instance_count",
            "instances": [{"instance_id": name + "-instance", "evidence_ids": [eid]}],
            "formula": {
                "expression": "W*L*Q/1000000",
                "terms": [
                    {
                        "symbol": "W",
                        "value": 173,
                        "unit": "mm",
                        "role": "projection_width",
                        "evidence_ids": [eid],
                    },
                    {
                        "symbol": "L",
                        "value": 421,
                        "unit": "mm",
                        "role": "projection_length",
                        "evidence_ids": [eid],
                    },
                    {
                        "symbol": "Q",
                        "value": 1,
                        "unit": "count",
                        "role": "physical_instance_count",
                        "evidence_ids": [eid],
                    },
                ],
                "result": 173 * 421 / 1_000_000,
                "output_unit": "m2",
            },
            "evidence": [
                {
                    "evidence_id": eid,
                    "source_id": "source-a",
                    "layout": "Model",
                    "stage": "elevation",
                    "role": "closeup",
                    "state": "CANDIDATE",
                    "handles": ["SYN-X", "SYN-Y"],
                    "bbox": [-11, 7, 162, 428],
                    "image": {**self.ref(self.image), "pixels": [3, 2]},
                }
            ],
            "stage_states": {"plan": "MISSING", "elevation": "CANDIDATE", "detail": "MISSING"},
            "state": "REVIEW",
            "unknowns": ["No plan binding.", "No detail binding."],
        }

    def freeze(self, prediction=None, manifest=None):
        return pilot.freeze(
            manifest or self.manifest, prediction or self.prediction, [str(self.code)], self.base
        )

    def test_valid_full_repeat_and_no_accuracy_claim(self):
        a = self.freeze()
        b = self.freeze()
        result = pilot.compare(a, b)
        self.assertEqual(result["status"], "REPEATED")
        self.assertTrue(result["repeatable_nonempty"])
        self.assertFalse(result["accuracy_evaluated"])
        self.assertFalse(a["limitations"]["blindness_guaranteed"])
        self.assertTrue(a["limitations"]["code_may_embed_answers"])

    def test_reordering_components_instances_terms_and_handles_is_not_drift(self):
        p = copy.deepcopy(self.prediction)
        p["components"].append(self.component("object-b"))
        a = self.freeze(p)
        p["components"].reverse()
        for c in p["components"]:
            c["formula"]["terms"].reverse()
            c["evidence"][0]["handles"].reverse()
        self.assertEqual(pilot.compare(a, self.freeze(p))["status"], "REPEATED")

    def test_extra_component_even_when_original_amount_unchanged(self):
        a = self.freeze()
        p = copy.deepcopy(self.prediction)
        p["components"].append(self.component("object-extra"))
        result = pilot.compare(a, self.freeze(p))
        self.assertEqual(result["extra_in_second"], ["object-extra"])
        self.assertEqual(result["status"], "DIFFERENT")

    def test_missing_component_not_hidden_by_equal_aggregate(self):
        p = copy.deepcopy(self.prediction)
        p["components"].append(self.component("object-b"))
        a = self.freeze(p)
        p["components"].pop()
        result = pilot.compare(a, self.freeze(p))
        self.assertEqual(result["missing_in_second"], ["object-b"])

    def test_instance_identity_change_detected(self):
        a = self.freeze()
        p = copy.deepcopy(self.prediction)
        p["components"][0]["instances"][0]["instance_id"] = "different-instance"
        self.assertIn(
            "instances", pilot.compare(a, self.freeze(p))["changed_components"]["object-a"]
        )

    def test_evidence_handle_and_bbox_change_detected(self):
        a = self.freeze()
        p = copy.deepcopy(self.prediction)
        p["components"][0]["evidence"][0]["handles"] = ["another-handle"]
        p["components"][0]["evidence"][0]["bbox"] = [-10, 7, 163, 428]
        self.assertIn(
            "evidence", pilot.compare(a, self.freeze(p))["changed_components"]["object-a"]
        )

    def test_unknowns_and_stage_state_are_not_discarded(self):
        a = self.freeze()
        p = copy.deepcopy(self.prediction)
        p["components"][0]["stage_states"]["detail"] = "NOT_APPLICABLE_CLAIMED"
        p["components"][0]["unknowns"] = ["No plan binding.", "No detail asserted, not audited."]
        changes = pilot.compare(a, self.freeze(p))["changed_components"]["object-a"]
        self.assertIn("stage_states", changes)
        self.assertIn("unknowns", changes)

    def test_null_to_zero_is_not_a_successful_fill(self):
        p = copy.deepcopy(self.prediction)
        p["components"][0]["fields"]["quantity"] = 0
        with self.assertRaises(pilot.ContractError):
            self.freeze(p)

    def test_empty_is_never_repeatable_nonempty(self):
        p = copy.deepcopy(self.prediction)
        p["components"] = []
        a = self.freeze(p)
        result = pilot.compare(a, self.freeze(p))
        self.assertEqual(a["status"], "EMPTY")
        self.assertEqual(result["status"], "EMPTY")
        self.assertFalse(result["repeatable_nonempty"])

    def test_two_empty_runs_preserve_different_failure_states(self):
        p = copy.deepcopy(self.prediction)
        p["components"] = []
        a = self.freeze(p)
        p["source_audit"][0].update(state="ERROR", reason="Synthetic decoder failure")
        result = pilot.compare(a, self.freeze(p))
        self.assertEqual(result["status"], "EMPTY")
        self.assertFalse(result["normalized_equal"])
        self.assertFalse(result["noncomponent_state_equal"])

    def test_malformed_enum_and_reference_types_are_contract_errors(self):
        p = copy.deepcopy(self.prediction)
        p["components"][0]["quantity_role"] = []
        with self.assertRaises(pilot.ContractError):
            self.freeze(p)
        p = copy.deepcopy(self.prediction)
        p["components"][0]["instances"][0]["evidence_ids"] = [{}]
        with self.assertRaises(pilot.ContractError):
            self.freeze(p)

    def test_duplicate_evidence_reference_rejected(self):
        p = copy.deepcopy(self.prediction)
        p["components"][0]["instances"][0]["evidence_ids"] *= 2
        with self.assertRaises(pilot.ContractError):
            self.freeze(p)

    def test_source_drift_rejects_old_freeze(self):
        a = self.freeze()
        self.cad.write_bytes(self.cad.read_bytes() + b"999\nchanged\n")
        with self.assertRaises(pilot.ContractError):
            pilot.verify_frozen(a)

    def test_code_drift_rejects_old_freeze(self):
        a = self.freeze()
        self.code.write_text("# changed\n", encoding="utf8")
        with self.assertRaises(pilot.ContractError):
            pilot.verify_frozen(a)

    def test_policy_drift_rejects_old_freeze(self):
        a = self.freeze()
        self.policy.write_text("{}", encoding="utf8")
        with self.assertRaises(pilot.ContractError):
            pilot.verify_frozen(a)

    def test_missing_image_rejected(self):
        self.image.unlink()
        with self.assertRaises(pilot.ContractError):
            self.freeze()

    def test_bad_image_signature_even_with_current_hash(self):
        self.image.write_bytes(b"not a picture")
        self.prediction["components"][0]["evidence"][0]["image"].update(self.ref(self.image))
        with self.assertRaises(pilot.ContractError):
            self.freeze()

    def test_bad_png_crc_even_with_current_hash(self):
        data = bytearray(png())
        data[-1] ^= 1
        self.image.write_bytes(data)
        self.prediction["components"][0]["evidence"][0]["image"].update(self.ref(self.image))
        with self.assertRaises(pilot.ContractError):
            self.freeze()

    def test_png_pixel_metadata_is_checked(self):
        self.prediction["components"][0]["evidence"][0]["image"]["pixels"] = [300, 200]
        with self.assertRaises(pilot.ContractError):
            self.freeze()

    def test_png_nonconsecutive_idat_is_rejected(self):
        self.image.write_bytes(png(between=[png_chunk(b"tEXt", b"Comment\0synthetic")]))
        self.prediction["components"][0]["evidence"][0]["image"].update(self.ref(self.image))
        with self.assertRaisesRegex(pilot.ContractError, "IDAT chunks must be consecutive"):
            self.freeze()

    def test_png_consecutive_idat_with_surrounding_metadata_is_valid(self):
        self.image.write_bytes(
            png(
                before=[png_chunk(b"tEXt", b"Comment\0synthetic")],
                between=[],
                after=[png_chunk(b"tEXt", b"Review\0synthetic")],
            )
        )
        self.prediction["components"][0]["evidence"][0]["image"].update(self.ref(self.image))
        self.assertEqual(self.freeze()["status"], "PREDICTION_FROZEN")

    def test_png_bad_critical_chunks_palette_and_transparency_are_rejected(self):
        palette = png_chunk(b"PLTE", bytes([31, 71, 121]))
        transparency = png_chunk(b"tRNS", struct.pack(">3H", 31, 71, 121))
        invalid = {
            "unknown critical": png(before=[png_chunk(b"ABCD", b"")]),
            "reserved type bit": png(before=[png_chunk(b"abca", b"")]),
            "nonletter type": png(before=[png_chunk(b"a1CD", b"")]),
            "late palette": png(after=[palette]),
            "duplicate palette": png(before=[palette, palette]),
            "malformed palette": png(before=[png_chunk(b"PLTE", b"\0")]),
            "grayscale palette": png(color=0, before=[palette]),
            "palette after transparency": png(color=2, before=[transparency, palette]),
            "alpha transparency": png(before=[transparency]),
            "malformed transparency": png(color=2, before=[png_chunk(b"tRNS", b"\0")]),
            "duplicate transparency": png(color=2, before=[transparency, transparency]),
            "late transparency": png(color=2, after=[transparency]),
            "out-of-range transparency": png(
                color=0, before=[png_chunk(b"tRNS", struct.pack(">H", 256))]
            ),
        }
        for label, data in invalid.items():
            self.image.write_bytes(data)
            self.prediction["components"][0]["evidence"][0]["image"].update(self.ref(self.image))
            with self.subTest(label=label), self.assertRaises(pilot.ContractError):
                self.freeze()

    def test_png_valid_supported_colors_palette_and_transparency(self):
        valid = [
            png(color=0, before=[png_chunk(b"tRNS", struct.pack(">H", 31))]),
            png(
                color=2,
                before=[
                    png_chunk(b"PLTE", bytes([31, 71, 121])),
                    png_chunk(b"tRNS", struct.pack(">3H", 31, 71, 121)),
                ],
            ),
            png(color=4),
            png(color=6, before=[png_chunk(b"PLTE", bytes([31, 71, 121]))]),
        ]
        for index, data in enumerate(valid):
            self.image.write_bytes(data)
            self.prediction["components"][0]["evidence"][0]["image"].update(self.ref(self.image))
            with self.subTest(index=index):
                self.assertEqual(self.freeze()["status"], "PREDICTION_FROZEN")

    def test_new_image_folder_is_normalized_by_real_bytes(self):
        a = self.freeze()
        p = copy.deepcopy(self.prediction)
        other = self.base / "second.png"
        other.write_bytes(png())
        p["components"][0]["evidence"][0]["image"].update(self.ref(other))
        self.assertEqual(pilot.compare(a, self.freeze(p))["status"], "REPEATED")

    def test_quantity_role_changes_not_erased_when_total_same(self):
        a = self.freeze()
        p = copy.deepcopy(self.prediction)
        p["components"][0]["quantity_role"] = "billable_face_count"
        self.assertIn(
            "quantity_role", pilot.compare(a, self.freeze(p))["changed_components"]["object-a"]
        )

    def test_physical_quantity_must_match_explicit_instances(self):
        p = copy.deepcopy(self.prediction)
        p["components"][0]["fields"]["quantity"] = 2
        with self.assertRaises(pilot.ContractError):
            self.freeze(p)

    def test_quantity_requires_typed_role(self):
        p = copy.deepcopy(self.prediction)
        p["components"][0]["quantity_role"] = None
        with self.assertRaises(pilot.ContractError):
            self.freeze(p)

    def test_formula_count_requires_typed_role(self):
        p = copy.deepcopy(self.prediction)
        p["components"][0]["formula"]["terms"][2]["role"] = "material_label_count"
        with self.assertRaises(pilot.ContractError):
            self.freeze(p)

    def test_typed_count_roles_cannot_disguise_fractional_counts_as_other_units(self):
        for role in pilot.QUANTITY_ROLES:
            for unit in ["ratio", "mm", "m", "m2"]:
                p = copy.deepcopy(self.prediction)
                c = p["components"][0]
                c["formula"]["terms"][2].update(role=role, unit=unit, value=2.5)
                c["formula"]["result"] *= 2.5
                c["fields"]["engineering_quantity"] *= 2.5
                with (
                    self.subTest(role=role, unit=unit),
                    self.assertRaisesRegex(
                        pilot.ContractError, "count units and typed quantity roles must agree"
                    ),
                ):
                    self.freeze(p)

    def test_same_role_formula_count_must_match_nonphysical_visible_quantity(self):
        p = copy.deepcopy(self.prediction)
        c = p["components"][0]
        c["quantity_role"] = "billable_face_count"
        c["formula"]["terms"][2].update(role="billable_face_count", value=2)
        c["formula"]["result"] *= 2
        c["fields"]["engineering_quantity"] *= 2
        with self.assertRaisesRegex(pilot.ContractError, "same-role visible quantity"):
            self.freeze(p)

    def test_formula_count_cannot_disagree_with_instance_inventory(self):
        p = copy.deepcopy(self.prediction)
        p["components"][0]["formula"]["terms"][2]["value"] = 2
        p["components"][0]["formula"]["result"] *= 2
        p["components"][0]["fields"]["engineering_quantity"] *= 2
        with self.assertRaises(pilot.ContractError):
            self.freeze(p)

    def test_formula_recomputed_not_trusted(self):
        p = copy.deepcopy(self.prediction)
        p["components"][0]["formula"]["result"] += 1
        with self.assertRaises(pilot.ContractError):
            self.freeze(p)

    def test_formula_no_eval_calls_or_unbound_constants(self):
        for expression in ["__import__('os').getcwd()", "X*W", "0.072833"]:
            p = copy.deepcopy(self.prediction)
            p["components"][0]["formula"]["expression"] = expression
            with self.subTest(expression=expression), self.assertRaises(pilot.ContractError):
                self.freeze(p)

    def test_cross_component_formula_reference_rejected(self):
        p = copy.deepcopy(self.prediction)
        p["components"][0]["formula"]["terms"][0]["evidence_ids"] = ["foreign-evidence"]
        with self.assertRaises(pilot.ContractError):
            self.freeze(p)

    def test_duplicate_components_and_instances_rejected(self):
        p = copy.deepcopy(self.prediction)
        p["components"].append(copy.deepcopy(p["components"][0]))
        with self.assertRaises(pilot.ContractError):
            self.freeze(p)
        p["components"][1] = self.component("object-b")
        p["components"][1]["instances"][0]["instance_id"] = p["components"][0]["instances"][0][
            "instance_id"
        ]
        with self.assertRaises(pilot.ContractError):
            self.freeze(p)

    def test_manifest_rejects_all_extra_selector_fields(self):
        for key in [
            "gold",
            "workbook",
            "old_findings",
            "human_rowno",
            "human_screenshot",
            "bbox",
            "handle_selector",
            "query",
        ]:
            m = copy.deepcopy(self.manifest)
            m[key] = "not permitted"
            with self.subTest(key=key), self.assertRaises(pilot.ContractError):
                self.freeze(manifest=m)

    def test_nested_source_selector_rejected(self):
        m = copy.deepcopy(self.manifest)
        m["sources"][0]["bbox"] = [0, 0, 1, 1]
        with self.assertRaises(pilot.ContractError):
            self.freeze(manifest=m)

    def test_renamed_workbook_is_not_cad(self):
        self.cad.write_bytes(b"PK\x03\x04pretend workbook")
        self.manifest["sources"][0].update(self.ref(self.cad))
        with self.assertRaises(pilot.ContractError):
            self.freeze()

    def test_policy_target_key_rejected(self):
        p = json.loads(self.policy.read_text("utf8"))
        p["rules"][0]["rowno"] = 8
        self.policy.write_text(json.dumps(p), encoding="utf8")
        self.manifest["policy_files"] = [self.ref(self.policy)]
        with self.assertRaises(pilot.ContractError):
            self.freeze()

    def test_confirmed_policy_requires_approval(self):
        p = json.loads(self.policy.read_text("utf8"))
        p["scope"] = "confirmed_policy"
        self.policy.write_text(json.dumps(p), encoding="utf8")
        self.manifest["policy_files"] = [self.ref(self.policy)]
        with self.assertRaises(pilot.ContractError):
            self.freeze()

    def test_general_policy_cannot_hide_structured_targets_in_approval(self):
        policy = json.loads(self.policy.read_text("utf8"))
        for approval in [
            None,
            {"target_row": 123, "bbox": [1, 2, 3, 4], "gold": "synthetic_answer.xlsx"},
            {"by": "synthetic", "at": "synthetic", "basis": "synthetic"},
        ]:
            policy["approval"] = approval
            self.policy.write_text(json.dumps(policy), encoding="utf8")
            self.manifest["policy_files"] = [self.ref(self.policy)]
            with (
                self.subTest(approval=approval),
                self.assertRaisesRegex(
                    pilot.ContractError, "general policy cannot include approval"
                ),
            ):
                self.freeze()

    def test_confirmed_policy_approval_has_exact_nonempty_text_fields(self):
        policy = json.loads(self.policy.read_text("utf8"))
        policy.update(
            scope="confirmed_policy",
            approval={"by": "synthetic", "at": "synthetic", "basis": "rule"},
        )
        self.policy.write_text(json.dumps(policy), encoding="utf8")
        self.manifest["policy_files"] = [self.ref(self.policy)]
        self.assertEqual(self.freeze()["status"], "PREDICTION_FROZEN")
        for approval in [
            {"by": "synthetic", "at": "synthetic", "basis": "rule", "target_row": 123},
            {"by": "synthetic", "at": "synthetic", "basis": {"bbox": [1, 2, 3, 4]}},
            {"by": "", "at": "synthetic", "basis": "rule"},
        ]:
            policy["approval"] = approval
            self.policy.write_text(json.dumps(policy), encoding="utf8")
            self.manifest["policy_files"] = [self.ref(self.policy)]
            with self.subTest(approval=approval), self.assertRaises(pilot.ContractError):
                self.freeze()

    def test_source_audit_missing_extra_duplicate_rejected(self):
        for audits in [
            [],
            [{"source_id": "wrong", "state": "SCANNED", "reason": ""}],
            self.prediction["source_audit"] * 2,
        ]:
            p = copy.deepcopy(self.prediction)
            p["source_audit"] = audits
            with self.subTest(audits=audits), self.assertRaises(pilot.ContractError):
                self.freeze(p)

    def test_pass_claim_and_missing_stage_hidden_as_candidate_rejected(self):
        p = copy.deepcopy(self.prediction)
        p["components"][0]["state"] = "PASS"
        with self.assertRaises(pilot.ContractError):
            self.freeze(p)
        p = copy.deepcopy(self.prediction)
        p["components"][0]["stage_states"]["plan"] = "CANDIDATE"
        with self.assertRaises(pilot.ContractError):
            self.freeze(p)

    def test_frozen_content_cannot_be_silently_edited(self):
        a = self.freeze()
        a["predictions"]["components"][0]["unknowns"] = []
        with self.assertRaises(pilot.ContractError):
            pilot.verify_frozen(a)

    def test_access_declarations_are_enforced_but_prior_context_is_honest(self):
        for key in [
            "numeric_gold_accessed",
            "human_screenshots_accessed",
            "old_predictions_accessed",
        ]:
            p = copy.deepcopy(self.prediction)
            p["declared_access"][key] = True
            with self.subTest(key=key), self.assertRaises(pilot.ContractError):
                self.freeze(p)
        self.assertTrue(self.freeze()["predictions"]["declared_access"]["prior_labels_in_context"])

    def test_distinct_input_context_not_called_repeatable(self):
        a = self.freeze()
        m = copy.deepcopy(self.manifest)
        m["experiment_id"] = "another-experiment"
        self.assertEqual(pilot.compare(a, self.freeze(manifest=m))["status"], "CONTEXT_DRIFT")

    def test_output_never_overwrites_existing_artifact(self):
        destination = self.base / "frozen.json"
        pilot.write_new_json(destination, self.freeze())
        with self.assertRaises(FileExistsError):
            pilot.write_new_json(destination, {})

    def test_json_duplicate_keys_and_nan_rejected(self):
        bad = self.base / "bad.json"
        for value in ['{"x":1,"x":2}', '{"x":NaN}']:
            bad.write_text(value, encoding="utf8")
            with self.assertRaises(pilot.ContractError):
                pilot.load_json(bad)

    def test_oversized_integer_fields_terms_and_bbox_are_contract_errors(self):
        for location in ["field", "term", "bbox"]:
            p = copy.deepcopy(self.prediction)
            c = p["components"][0]
            if location == "field":
                c["fields"]["width_mm"] = 10**400
            elif location == "term":
                c["formula"]["terms"][0]["value"] = 10**400
            else:
                c["evidence"][0]["bbox"][2] = 10**400
            with self.subTest(location=location), self.assertRaises(pilot.ContractError):
                self.freeze(p)

    def test_formula_oversized_constant_and_intermediate_are_contract_errors(self):
        for expression, bindings in [(str(10**400) + "*T", {"T": 1}), ("T*T", {"T": 10**200})]:
            with self.subTest(expression=expression), self.assertRaises(pilot.ContractError):
                pilot.formula_value(expression, bindings)

    def test_cli_oversized_integer_reports_invalid_without_traceback(self):
        manifest_path = self.base / "manifest.json"
        prediction_path = self.base / "prediction.json"
        destination = self.base / "frozen.json"
        manifest_path.write_text(json.dumps(self.manifest), encoding="utf8")
        self.prediction["components"][0]["fields"]["width_mm"] = 10**400
        prediction_path.write_text(json.dumps(self.prediction), encoding="utf8")
        result = subprocess.run(
            [
                sys.executable,
                "-B",
                str(MODULE_PATH),
                "freeze",
                str(manifest_path),
                str(prediction_path),
                "--code",
                str(self.code),
                "--out",
                str(destination),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stdout)["status"], "INVALID")
        self.assertEqual(result.stderr, "")
        self.assertFalse(destination.exists())

    def test_limitation_payload_does_not_mutate_module_constant(self):
        a = self.freeze()
        a["limitations"]["blindness_guaranteed"] = True
        self.assertFalse(pilot.LIMITATIONS["blindness_guaranteed"])


if __name__ == "__main__":
    unittest.main()
