"""Fresh-process synthetic validation; no customer data is opened by these tests."""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

import audit_replay as audit

SCRIPT = Path(audit.__file__).resolve()


class ReplayAuditTests(unittest.TestCase):
    def run_case(self, code, *, old_out=False, source_extra=None, extra_args=(), hardlink=False):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "project"
            root.mkdir()
            out = root / "outputs/current-run"
            out.mkdir(parents=True)
            (root / "input.dxf").write_text("synthetic vector drawing", encoding="utf-8")
            (root / "query.json").write_text('{"family": "synthetic"}', encoding="utf-8")
            (root / "gold.json").write_text('{"answer": 123}', encoding="utf-8")
            (root / "human.xlsx").write_bytes(b"synthetic workbook")
            (root / "outputs/old.json").write_text('{"answer": 123}', encoding="utf-8")
            (root / "deps").mkdir()
            (root / "deps/leak.json").write_text('{"answer": 123}', encoding="utf-8")
            if old_out:
                (out / "old.json").write_text('{"answer": 123}', encoding="utf-8")
            if hardlink:
                os.link(root / "input.dxf", out / "hardlink.dxf")
            target = root / "target.py"
            target.write_text(code, encoding="utf-8")
            report = out / "audit.json"
            args = [
                sys.executable,
                "-B",
                str(SCRIPT),
                "--project-root",
                str(root),
                "--script",
                str(target),
                "--source",
                str(root / "input.dxf"),
                "--source",
                str(root / "query.json"),
                "--report",
                str(report),
                "--out-root",
                str(out),
            ]
            if source_extra:
                args.extend(["--source", str(root / source_extra)])
            args.extend(["--", *extra_args])
            result = subprocess.run(
                args, capture_output=True, text=True, encoding="utf-8", timeout=30
            )
            payload = json.loads(report.read_text(encoding="utf-8")) if report.exists() else None
            return result, payload

    def assert_denied(self, code, **kwargs):
        result, payload = self.run_case(code, **kwargs)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIsNotNone(payload, result.stdout + result.stderr)
        self.assertEqual(payload["status"], "AUDITED_FAIL")
        self.assertTrue(payload["violations"])
        self.assertFalse(payload["events_truncated"])
        return payload

    def test_allow_sources_generated_outputs_and_argv(self):
        result, payload = self.run_case(
            "from pathlib import Path\nimport json, sys\n"
            "assert sys.argv[1:] == ['alpha', '--flag']\n"
            "assert Path('input.dxf').read_text() == 'synthetic vector drawing'\n"
            "assert json.loads(Path('query.json').read_text())['family'] == 'synthetic'\n"
            "p = Path('outputs/current-run/result.json')\n"
            "p.write_text('{\"ok\": true}')\nassert json.loads(p.read_text())['ok']\n",
            extra_args=("alpha", "--flag"),
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(payload["status"], "AUDITED_PASS")
        self.assertTrue(all(item["unchanged"] for item in payload["source_hashes"]))
        self.assertEqual(
            {
                event["phase"]
                for event in payload["events"]
                if event.get("basis") == "explicit_source"
            },
            {"source_hash_before", "target", "source_hash_after"},
        )
        self.assertTrue(payload["project_data_reads"])
        self.assertFalse(payload["is_blind"])

    def test_forbidden_reads_and_no_fake_deps_exemption(self):
        for filename in ("gold.json", "human.xlsx", "outputs/old.json", "deps/leak.json"):
            with self.subTest(filename=filename):
                payload = self.assert_denied(f"open({filename!r}, 'rb').read()\n")
                self.assertIn(filename.replace("/", os.sep), payload["violations"][0]["path"])

    def test_caught_rejections_still_fail_and_retain_all_events(self):
        payload = self.assert_denied(
            "for _ in range(120):\n"
            " try:\n  open('gold.json').read()\n except PermissionError:\n  pass\n"
        )
        self.assertEqual(len(payload["violations"]), 120)
        self.assertEqual(payload["target_exit_code"], 0)

    def test_os_open_flags_and_fd_wrapping(self):
        for flags in ("os.O_RDONLY", "os.O_RDWR", "os.O_WRONLY", "os.O_TRUNC | os.O_RDONLY"):
            with self.subTest(flags=flags):
                self.assert_denied(f"import os\nfd = os.open('gold.json', {flags})\n")
        result, payload = self.run_case(
            "import os\nfd = os.open(os.path.abspath('input.dxf'), os.O_RDONLY)\n"
            "with os.fdopen(fd, 'rb') as stream:\n assert stream.read()\n"
            "p = os.path.abspath('outputs/current-run/new.json')\n"
            "fd = os.open(p, os.O_RDWR | os.O_CREAT)\n"
            "with os.fdopen(fd, 'w+b') as stream:\n stream.write(b'{}')\n"
            " stream.seek(0)\n assert stream.read() == b'{}'\n"
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(payload["status"], "AUDITED_PASS")

    def test_relative_os_open_cannot_hide_a_directory_descriptor(self):
        payload = self.assert_denied("import os\nos.open('input.dxf', os.O_RDONLY)\n")
        self.assertTrue(any("ambiguous dir_fd" in item["reason"] for item in payload["violations"]))

    def test_exact_platform_null_device_is_not_project_data(self):
        result, payload = self.run_case(
            "import os\n"
            "fd = os.open(os.devnull, os.O_RDWR)\n"
            "assert os.read(fd, 8) == b''\n"
            "assert os.write(fd, b'synthetic') == 9\n"
            "os.close(fd)\n"
            "with open(os.devnull, 'wb') as stream:\n stream.write(b'synthetic')\n"
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        null_events = [event for event in payload["events"] if event.get("basis") == "null_device"]
        self.assertEqual(len(null_events), 2)
        self.assertTrue(all(event["decision"] == "ALLOWED" for event in null_events))
        self.assertTrue(all(event["filesystem_data"] is False for event in null_events))
        self.assertFalse(payload["project_data_reads"])

    def test_null_device_exception_rejects_lookalikes_and_descriptors(self):
        self.assertTrue(audit.is_platform_null_device(os.devnull))
        for value in ("null", "nul.json", "NUL.json", "deps/nul", "./nul", "CON", 0, 2):
            with self.subTest(value=value):
                self.assertFalse(audit.is_platform_null_device(value))
        self.assert_denied("import os\nos.open('nul.json', os.O_RDWR | os.O_CREAT)\n")
        if os.name == "nt":
            self.assertTrue(audit.is_platform_null_device("NUL"))

    @unittest.skipUnless(os.name == "nt", "Windows-native runtime compatibility")
    def test_windows_version_probe_needs_no_child_process_or_pipe(self):
        result, payload = self.run_case(
            "import platform, sys\n"
            "version = platform.win32_ver()[1]\n"
            "assert version and all(part.isdigit() for part in version.split('.'))\n"
            "native = platform._syscmd_ver()[2]\n"
            "assert native == '.'.join(map(str, sys.getwindowsversion().platform_version))\n"
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(payload["violations"])
        self.assertTrue(
            any(event["event"] == "runtime_compatibility" for event in payload["events"])
        )

    def test_pipe_descriptors_do_not_gain_null_device_exception(self):
        self.assert_denied("import os\nr, w = os.pipe()\nos.fdopen(r, 'rb')\n")

    def test_startup_package_root_does_not_exempt_project_json(self):
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            packages = root / "site-packages"
            packages.mkdir()
            with patch.object(audit.sys, "path", [str(packages), str(root / "arbitrary")]):
                runtime = audit.runtime_roots()
            self.assertIn(packages, runtime)
            self.assertNotIn(root / "arbitrary", runtime)
            # Exercise the project-first policy without installing a global hook.
            boundary = object.__new__(audit.Boundary)
            boundary.root, boundary.out_root = root, root / "out"
            boundary.sources, boundary.created = set(), set()
            boundary.runtime = runtime
            self.assertIsNone(boundary.readable(packages / "private.json"))

    def test_runtime_dxf_and_plot_libraries_use_current_output(self):
        result, payload = self.run_case(
            "from pathlib import Path\nimport ezdxf\n"
            "from matplotlib.figure import Figure\n"
            "from matplotlib.backends.backend_agg import FigureCanvasAgg\n"
            "doc = ezdxf.new()\n"
            "doc.modelspace().add_line((0, 0), (10, 20))\n"
            "doc.saveas('outputs/current-run/generated.dxf')\n"
            "assert len(ezdxf.readfile('outputs/current-run/generated.dxf').modelspace()) == 1\n"
            "fig = Figure()\nFigureCanvasAgg(fig)\n"
            "fig.add_subplot().plot([0, 1], [0, 1])\n"
            "fig.savefig('outputs/current-run/plot.png')\n"
            "assert Path('outputs/current-run/plot.png').stat().st_size > 100\n"
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(payload["status"], "AUDITED_PASS")

    def test_sources_cannot_be_mutated_and_hashes_stay_intact(self):
        for code in (
            "open('input.dxf', 'r+')\n",
            "open('query.json', 'w')\n",
            "import os\nos.remove('input.dxf')\n",
        ):
            with self.subTest(code=code):
                payload = self.assert_denied(code)
                self.assertTrue(all(item["unchanged"] for item in payload["source_hashes"]))

    def test_old_outputs_cannot_be_read_or_appended(self):
        for mode in ("r", "r+", "a", "a+"):
            with self.subTest(mode=mode):
                self.assert_denied(
                    f"open('outputs/current-run/old.json', {mode!r})\n", old_out=True
                )
        result, payload = self.run_case(
            "with open('outputs/current-run/old.json', 'w+') as f:\n"
            " f.write('{}')\n f.seek(0)\n assert f.read() == '{}'\n",
            old_out=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(payload["status"], "AUDITED_PASS")

    def test_outside_writes_moves_links_and_subprocess_are_denied(self):
        cases = [
            "open('unapproved.json', 'w')\n",
            "import os\nos.mkdir('unapproved')\n",
            "import os\nos.rename('gold.json', 'outputs/current-run/moved.json')\n",
            "import os\nos.link('input.dxf', 'outputs/current-run/link.dxf')\n",
            "import os\nos.symlink('input.dxf', 'outputs/current-run/link.dxf')\n",
            "import subprocess\nsubprocess.run(['anything'])\n",
        ]
        for code in cases:
            with self.subTest(code=code):
                self.assert_denied(code)

    def test_allowlist_rejects_gold_outputs_and_spreadsheets(self):
        for source in ("gold.json", "outputs/old.json", "human.xlsx"):
            with self.subTest(source=source):
                result, payload = self.run_case("pass\n", source_extra=source)
                self.assertEqual(result.returncode, 2)
                self.assertIsNone(payload)
                self.assertIn("AUDIT_SETUP_FAILED", result.stdout)

    def test_target_exception_retains_full_traceback(self):
        result, payload = self.run_case("raise RuntimeError('synthetic-target-failure')\n")
        self.assertEqual(result.returncode, 2)
        self.assertIn("synthetic-target-failure", payload["target_error"]["traceback"])
        self.assertFalse(payload["events_truncated"])

    def test_missing_read_probes_cannot_read_any_bytes(self):
        result, payload = self.run_case(
            "try:\n open('missing-private.json').read()\nexcept FileNotFoundError:\n pass\n"
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        probes = [event for event in payload["events"] if event["decision"] == "NO_READ"]
        self.assertTrue(any(event["path"].endswith("missing-private.json") for event in probes))
        self.assertFalse(payload["project_data_reads"])

    def test_preexisting_output_hardlink_cannot_modify_source(self):
        payload = self.assert_denied(
            "open('outputs/current-run/hardlink.dxf', 'w').write('changed')\n", hardlink=True
        )
        self.assertTrue(all(item["unchanged"] for item in payload["source_hashes"]))

    def test_integer_flags_read_write_classification(self):
        self.assertEqual(audit.open_access(None, os.O_RDONLY), (True, False, False))
        self.assertEqual(audit.open_access(None, os.O_WRONLY), (False, True, False))
        self.assertEqual(audit.open_access(None, os.O_RDWR), (True, True, False))
        self.assertEqual(audit.open_access("w+", os.O_RDWR | os.O_TRUNC), (True, True, True))
        for mode, flags in ((None, None), (None, True), (None, -1), ("?", 0), ("rw", 0)):
            with self.subTest(mode=mode, flags=flags), self.assertRaises(ValueError):
                audit.open_access(mode, flags)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--receipt", type=Path)
    args = parser.parse_args()
    output = io.StringIO()
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(ReplayAuditTests)
    with contextlib.redirect_stderr(output):
        result = unittest.TextTestRunner(stream=output, verbosity=2).run(suite)
    print(output.getvalue())
    receipt = {
        "schema_version": "synthetic-audit-wrapper-validation/1.0",
        "created_at": datetime.now(UTC).isoformat(),
        "status": "PASS" if result.wasSuccessful() else "FAIL",
        "tests_run": result.testsRun,
        "failures": len(result.failures),
        "errors": len(result.errors),
        "customer_scripts_executed": False,
        "customer_sources_opened": False,
        "output": output.getvalue(),
        "wrapper_sha256": audit.file_hash(SCRIPT),
        "tests_sha256": audit.file_hash(Path(__file__)),
        "limitations": audit.LIMITATIONS,
    }
    if args.receipt:
        args.receipt.parent.mkdir(parents=True, exist_ok=True)
        args.receipt.write_text(
            json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
