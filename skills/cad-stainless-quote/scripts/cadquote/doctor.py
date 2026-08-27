"""Environment diagnostics for the CAD quotation pipeline."""

from __future__ import annotations

import importlib
import platform
import shutil
import sys
from dataclasses import asdict, dataclass
from importlib import metadata
from pathlib import Path
from typing import Any

from .converter import discover_converters

REQUIRED_DISTRIBUTIONS = {
    "ezdxf": "ezdxf",
    "openpyxl": "openpyxl",
    "cv2": "opencv-python-headless",
    "pydantic": "pydantic",
    "xlsxwriter": "XlsxWriter",
    "xlrd": "xlrd",
    "rarfile": "rarfile",
    "py7zr": "py7zr",
    "yaml": "PyYAML",
    "rapidfuzz": "RapidFuzz",
    "shapely": "shapely",
    "matplotlib": "matplotlib",
}


@dataclass(frozen=True, slots=True)
class DiagnosticCheck:
    name: str
    status: str
    detail: str
    required: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _find_command(name: str, known_paths: tuple[str, ...] = ()) -> str | None:
    located = shutil.which(name)
    if located:
        return str(Path(located).resolve())
    for value in known_paths:
        candidate = Path(value)
        if candidate.is_file():
            return str(candidate.resolve())
    return None


def run_doctor() -> dict[str, Any]:
    """Inspect the runtime without mutating it and return a JSON-ready report."""

    checks: list[DiagnosticCheck] = []
    python_ok = sys.version_info >= (3, 11)
    checks.append(
        DiagnosticCheck(
            name="python",
            status="PASS" if python_ok else "BLOCK",
            detail=f"{platform.python_implementation()} {platform.python_version()}",
        )
    )

    for module_name, distribution_name in REQUIRED_DISTRIBUTIONS.items():
        try:
            importlib.import_module(module_name)
            version = metadata.version(distribution_name)
        except Exception as exc:  # diagnostics must report every failed dependency
            checks.append(
                DiagnosticCheck(
                    name=f"python:{distribution_name}",
                    status="BLOCK",
                    detail=f"{type(exc).__name__}: {exc}",
                )
            )
        else:
            checks.append(
                DiagnosticCheck(
                    name=f"python:{distribution_name}",
                    status="PASS",
                    detail=version,
                )
            )

    converters = discover_converters()
    checks.append(
        DiagnosticCheck(
            name="dwg_converter",
            status="PASS" if converters else "REVIEW",
            detail=(
                "; ".join(f"{tool.kind}={tool.executable}" for tool in converters)
                if converters
                else "No ODA/AutoCAD/dwg2dxf converter found"
            ),
            required=False,
        )
    )

    seven_zip = _find_command(
        "7z",
        (
            r"C:\Program Files\7-Zip\7z.exe",
            r"C:\Program Files (x86)\7-Zip\7z.exe",
        ),
    )
    checks.append(
        DiagnosticCheck(
            name="archive_backend",
            status="PASS" if seven_zip else "REVIEW",
            detail=seven_zip or "7-Zip is required for advertised RAR and 7z extraction",
            required=False,
        )
    )

    required_failures = [
        check for check in checks if check.required and check.status not in {"PASS"}
    ]
    return {
        "status": "PASS" if not required_failures else "BLOCK",
        "platform": platform.platform(),
        "executable": sys.executable,
        "checks": [check.to_dict() for check in checks],
    }
