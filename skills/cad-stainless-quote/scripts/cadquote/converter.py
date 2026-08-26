"""Audited DWG to DXF conversion without modifying source drawings.

The module deliberately does not install a converter.  It discovers explicitly
configured tools first, then known local installations, and records a result for
every input DWG.  Each destination name includes the source hash, so drawings
with the same basename cannot overwrite one another.
"""

from __future__ import annotations

import glob
import hashlib
import os
import shutil
import subprocess
import tempfile
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .io import sha256_file


@dataclass(frozen=True, slots=True)
class ConverterTool:
    """A discovered command-line DWG converter."""

    kind: str
    executable: str
    origin: str
    priority: int

    @property
    def path(self) -> Path:
        return Path(self.executable)


@dataclass(slots=True)
class ConversionRecord:
    source: str
    destination: str
    source_sha256: str
    source_bytes: int
    status: str = "failed"
    converter: str | None = None
    output_sha256: str | None = None
    output_bytes: int | None = None
    elapsed_seconds: float = 0.0
    command: list[str] = field(default_factory=list)
    return_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    error: str | None = None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(slots=True)
class ConversionAudit:
    output_dir: str
    discovered_tools: list[ConverterTool]
    records: list[ConversionRecord]

    @property
    def expected_count(self) -> int:
        return len(self.records)

    @property
    def attempted_count(self) -> int:
        return sum(
            record.converter is not None and record.converter != "cached"
            for record in self.records
        )

    @property
    def succeeded_count(self) -> int:
        return sum(record.status == "converted" for record in self.records)

    @property
    def failed_count(self) -> int:
        return self.expected_count - self.succeeded_count

    def to_dict(self) -> dict[str, object]:
        return {
            "output_dir": self.output_dir,
            "expected_count": self.expected_count,
            "attempted_count": self.attempted_count,
            "succeeded_count": self.succeeded_count,
            "failed_count": self.failed_count,
            "discovered_tools": [asdict(tool) for tool in self.discovered_tools],
            "records": [record.to_dict() for record in self.records],
        }


_ENV_CANDIDATES: tuple[tuple[str, str], ...] = (
    ("oda", "CADQUOTE_ODA_CONVERTER"),
    ("oda", "ODA_CONVERTER"),
    ("autocad", "CADQUOTE_AUTOCAD_CONSOLE"),
    ("autocad", "AUTOCAD_CORE_CONSOLE"),
    ("dwg2dxf", "CADQUOTE_DWG2DXF"),
    ("dwg2dxf", "DWG2DXF"),
)


def _is_executable(path: str | Path) -> bool:
    candidate = Path(path).expanduser()
    if candidate.is_file():
        return os.name == "nt" or os.access(candidate, os.X_OK)
    return False


def _configured_candidates(
    explicit: Mapping[str, str | Path] | None,
    environ: Mapping[str, str],
) -> Iterable[ConverterTool]:
    for priority, (kind, variable) in enumerate(_ENV_CANDIDATES):
        value = environ.get(variable)
        if value and _is_executable(value):
            yield ConverterTool(kind, str(Path(value).resolve()), f"env:{variable}", priority)

    for offset, (kind, value) in enumerate((explicit or {}).items()):
        if kind not in {"oda", "autocad", "dwg2dxf"}:
            continue
        if _is_executable(value):
            yield ConverterTool(
                kind,
                str(Path(value).expanduser().resolve()),
                "explicit",
                -100 + offset,
            )


def _known_install_candidates() -> Iterable[ConverterTool]:
    patterns: list[tuple[str, str, int]] = []
    if os.name == "nt":
        program_roots = {
            os.environ.get("ProgramFiles", r"C:\Program Files"),
            os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
        }
        for root in sorted(program_roots):
            patterns.extend(
                [
                    (
                        "oda",
                        str(Path(root) / "ODA" / "ODAFileConverter*" / "ODAFileConverter.exe"),
                        20,
                    ),
                    ("oda", str(Path(root) / "ODAFileConverter*" / "ODAFileConverter.exe"), 21),
                    (
                        "autocad",
                        str(Path(root) / "Autodesk" / "AutoCAD *" / "accoreconsole.exe"),
                        30,
                    ),
                ]
            )
    elif sys_platform() == "darwin":
        patterns.append(
            (
                "oda",
                "/Applications/ODAFileConverter.app/Contents/MacOS/ODAFileConverter",
                20,
            )
        )
    else:
        patterns.extend(
            [
                ("oda", "/usr/bin/ODAFileConverter", 20),
                ("oda", "/usr/local/bin/ODAFileConverter", 21),
            ]
        )

    for kind, pattern, priority in patterns:
        for candidate in sorted(glob.glob(pattern)):
            if _is_executable(candidate):
                yield ConverterTool(kind, str(Path(candidate).resolve()), "known-install", priority)

    for name, kind, priority in (
        ("ODAFileConverter", "oda", 40),
        ("accoreconsole", "autocad", 50),
        ("dwg2dxf", "dwg2dxf", 60),
    ):
        candidate = shutil.which(name)
        if candidate and _is_executable(candidate):
            yield ConverterTool(kind, str(Path(candidate).resolve()), "PATH", priority)


def sys_platform() -> str:
    """Small seam kept separate for platform-aware tests."""

    import sys

    return sys.platform


def discover_converters(
    explicit: Mapping[str, str | Path] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> list[ConverterTool]:
    """Return usable converters in deterministic preference order.

    Explicit mappings and environment variables take precedence over known
    install locations.  Duplicate executable/kind pairs are removed.
    """

    environment = os.environ if environ is None else environ
    candidates = [
        *_configured_candidates(explicit, environment),
        *_known_install_candidates(),
    ]
    unique: dict[tuple[str, str], ConverterTool] = {}
    for tool in candidates:
        key = (tool.kind, os.path.normcase(tool.executable))
        previous = unique.get(key)
        if previous is None or tool.priority < previous.priority:
            unique[key] = tool
    return sorted(unique.values(), key=lambda tool: (tool.priority, tool.kind, tool.executable))


def collect_dwg_files(inputs: Path | str | Iterable[Path | str]) -> list[Path]:
    """Resolve DWG inputs without following directory symlinks."""

    values: Sequence[Path | str]
    if isinstance(inputs, (str, Path)):
        values = [inputs]
    else:
        values = list(inputs)

    found: dict[str, Path] = {}
    for value in values:
        path = Path(value).expanduser().resolve()
        if path.is_file() and path.suffix.casefold() == ".dwg":
            found[os.path.normcase(str(path))] = path
            continue
        if not path.is_dir():
            continue
        for root, dirs, files in os.walk(path, followlinks=False):
            dirs[:] = sorted(
                directory
                for directory in dirs
                if not (Path(root) / directory).is_symlink()
            )
            for name in sorted(files):
                candidate = Path(root) / name
                if candidate.suffix.casefold() == ".dwg" and not candidate.is_symlink():
                    resolved = candidate.resolve()
                    found[os.path.normcase(str(resolved))] = resolved
    return sorted(found.values(), key=lambda path: os.path.normcase(str(path)))


def unique_destination(source: Path, output_dir: Path, source_hash: str | None = None) -> Path:
    """Return a stable collision-free destination for one source drawing."""

    digest = source_hash or sha256_file(source)
    safe_stem = "".join(
        character if character not in '<>:"/\\|?*' else "_" for character in source.stem
    )
    safe_stem = (safe_stem.rstrip(" .") or "drawing")[:100]
    path_digest = hashlib.sha256(
        os.path.normcase(str(source.resolve())).encode("utf-8")
    ).hexdigest()[:8]
    return output_dir / f"{safe_stem}__{digest[:12]}_{path_digest}.dxf"


def _trim_output(value: str, limit: int = 20_000) -> str:
    if len(value) <= limit:
        return value
    return value[-limit:]


def _looks_like_dxf(path: Path) -> bool:
    try:
        if not path.is_file() or path.stat().st_size == 0:
            return False
        with path.open("rb") as handle:
            header = handle.read(512)
        return header.startswith(b"AutoCAD Binary DXF") or b"SECTION" in header.upper()
    except OSError:
        return False


def _run_command(command: list[str], timeout_seconds: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        errors="replace",
        timeout=timeout_seconds,
    )


def _convert_with_oda(
    tool: ConverterTool,
    source: Path,
    destination: Path,
    stage_dir: Path,
    timeout_seconds: int,
) -> tuple[list[str], subprocess.CompletedProcess[str], Path]:
    input_dir = stage_dir / "input"
    converted_dir = stage_dir / "output"
    input_dir.mkdir(parents=True)
    converted_dir.mkdir(parents=True)
    staged_source = input_dir / f"drawing{source.suffix.lower()}"
    # Never hard-link the original into a vendor tool's working directory: a
    # converter that writes in place would then mutate the user's source file.
    shutil.copy2(source, staged_source)
    command = [
        tool.executable,
        str(input_dir),
        str(converted_dir),
        "ACAD2018",
        "DXF",
        "0",
        "1",
        "*.dwg",
    ]
    completed = _run_command(command, timeout_seconds)
    produced = converted_dir / "drawing.dxf"
    return command, completed, produced


def _convert_with_dwg2dxf(
    tool: ConverterTool,
    source: Path,
    destination: Path,
    stage_dir: Path,
    timeout_seconds: int,
) -> tuple[list[str], subprocess.CompletedProcess[str], Path]:
    produced = stage_dir / "drawing.dxf"
    command = [tool.executable, "--as", "r2018", "-o", str(produced), str(source)]
    completed = _run_command(command, timeout_seconds)
    return command, completed, produced


def _convert_with_autocad(
    tool: ConverterTool,
    source: Path,
    destination: Path,
    stage_dir: Path,
    timeout_seconds: int,
) -> tuple[list[str], subprocess.CompletedProcess[str], Path]:
    produced = stage_dir / "drawing.dxf"
    script = stage_dir / "convert.scr"
    script.write_text(
        "_.FILEDIA\n0\n_.CMDDIA\n0\n_.-SAVEAS\n2018\n"
        f'"{produced}"\n_.QUIT\n',
        encoding="utf-8-sig",
    )
    command = [tool.executable, "/i", str(source), "/s", str(script)]
    completed = _run_command(command, timeout_seconds)
    return command, completed, produced


def _convert_one(
    tool: ConverterTool,
    source: Path,
    destination: Path,
    timeout_seconds: int,
) -> tuple[list[str], subprocess.CompletedProcess[str], Path, bool]:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".cadquote-convert-", dir=destination.parent) as temp:
        stage_dir = Path(temp)
        if tool.kind == "oda":
            command, completed, produced = _convert_with_oda(
                tool, source, destination, stage_dir, timeout_seconds
            )
        elif tool.kind == "autocad":
            command, completed, produced = _convert_with_autocad(
                tool, source, destination, stage_dir, timeout_seconds
            )
        elif tool.kind == "dwg2dxf":
            command, completed, produced = _convert_with_dwg2dxf(
                tool, source, destination, stage_dir, timeout_seconds
            )
        else:
            raise ValueError(f"Unsupported converter kind: {tool.kind}")

        produced_valid_dxf = completed.returncode == 0 and _looks_like_dxf(produced)
        if produced_valid_dxf:
            os.replace(produced, destination)
        return command, completed, destination, produced_valid_dxf


def convert_dwgs(
    inputs: Path | str | Iterable[Path | str],
    output_dir: Path | str,
    *,
    converter: ConverterTool | None = None,
    explicit_tools: Mapping[str, str | Path] | None = None,
    timeout_seconds: int = 600,
) -> ConversionAudit:
    """Convert every DWG and return a complete, non-throwing audit.

    Tool absence and individual conversion failures are represented as failed
    records.  This prevents a caller from silently treating a partial directory
    as a complete conversion.
    """

    destination_root = Path(output_dir).expanduser().resolve()
    destination_root.mkdir(parents=True, exist_ok=True)
    sources = collect_dwg_files(inputs)
    tools = discover_converters(explicit_tools)
    selected = converter or (tools[0] if tools else None)
    records: list[ConversionRecord] = []

    for source in sources:
        source_hash = sha256_file(source)
        destination = unique_destination(source, destination_root, source_hash)
        record = ConversionRecord(
            source=str(source),
            destination=str(destination),
            source_sha256=source_hash,
            source_bytes=source.stat().st_size,
        )
        records.append(record)
        if _looks_like_dxf(destination):
            record.status = "converted"
            record.converter = "cached"
            record.output_bytes = destination.stat().st_size
            record.output_sha256 = sha256_file(destination)
            continue
        if selected is None:
            record.error = (
                "No DWG converter found. Configure ODA File Converter, AutoCAD Core Console, "
                "or dwg2dxf."
            )
            continue

        record.converter = selected.kind
        started = time.monotonic()
        try:
            command, completed, produced, produced_valid_dxf = _convert_one(
                selected, source, destination, timeout_seconds
            )
            record.command = command
            record.return_code = completed.returncode
            record.stdout = _trim_output(completed.stdout)
            record.stderr = _trim_output(completed.stderr)
            if completed.returncode != 0:
                record.error = f"Converter exited with code {completed.returncode}."
            elif not produced_valid_dxf:
                record.error = "Converter reported success but produced no valid DXF header."
            else:
                record.status = "converted"
                record.output_bytes = produced.stat().st_size
                record.output_sha256 = sha256_file(produced)
        except subprocess.TimeoutExpired as exc:
            record.error = f"Conversion timed out after {timeout_seconds} seconds."
            record.stdout = _trim_output(exc.stdout or "")
            record.stderr = _trim_output(exc.stderr or "")
        except Exception as exc:  # conversion tools fail in many vendor-specific ways
            record.error = f"{type(exc).__name__}: {exc}"
        finally:
            record.elapsed_seconds = round(time.monotonic() - started, 3)

    return ConversionAudit(str(destination_root), tools, records)


# Compatibility-friendly singular name for callers processing one drawing.
def convert_dwg(
    source: Path | str,
    output_dir: Path | str,
    **kwargs: object,
) -> ConversionRecord:
    audit = convert_dwgs(source, output_dir, **kwargs)
    if not audit.records:
        raise FileNotFoundError(f"No DWG input found: {source}")
    return audit.records[0]
