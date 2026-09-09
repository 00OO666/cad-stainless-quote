"""Run a CAD replay with an explicit Python audit-event file boundary.

This is an observational Python boundary, not an OS sandbox or a blind test.
Native extensions can perform I/O without Python audit events. Code review is
still needed to exclude embedded answers and native/inherited-descriptor I/O.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import runpy
import site
import sys
import sysconfig
import tempfile
import traceback
from datetime import UTC, datetime
from pathlib import Path

CODE_SUFFIXES = {".py", ".pyc", ".pyd", ".so", ".dll"}
RUNTIME_DATA_SUFFIXES = {".ttf", ".otf", ".afm", ".pfb", ".mplstyle", ".pem"}
RUNTIME_METADATA_NAMES = {
    "METADATA",
    "WHEEL",
    "RECORD",
    "entry_points.txt",
    "top_level.txt",
    "matplotlibrc",
}
FORBIDDEN_SOURCE_PARTS = {"outputs", "gold", "gold-import", "gold-image-export"}
LIMITATIONS = [
    "Python audit events are not an OS sandbox; native-extension I/O may be unobserved.",
    "Operations on inherited descriptors without audit events are outside this proof.",
    "Permitted code/imports can embed data; this wrapper does not review code semantics.",
    "Allowlisted JSON is declared configuration; target-free content is not verified.",
    "Audit success does not establish blind prediction, CAD correctness, or accuracy.",
]


class BoundaryViolation(PermissionError):
    pass


def inside(path, root):
    return path == root or root in path.parents


def timestamp():
    return datetime.now(UTC).isoformat()


def as_path(value):
    return Path(os.fsdecode(value)).resolve()


def prepare_fd_resolver():
    """Load Windows handle functions before the hook; never guess an fd's path."""
    if os.name == "nt":
        import ctypes
        import msvcrt
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        function = kernel.GetFinalPathNameByHandleW
        function.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
        function.restype = wintypes.DWORD

        def resolve_fd(fd):
            buffer = ctypes.create_unicode_buffer(32768)
            handle = msvcrt.get_osfhandle(fd)
            size = function(handle, buffer, len(buffer), 0)
            if size == 0 or size >= len(buffer):
                raise OSError("descriptor does not resolve to a filesystem path")
            value = buffer.value
            if value.startswith("\\\\?\\UNC\\"):
                value = "\\\\" + value[8:]
            elif value.startswith("\\\\?\\"):
                value = value[4:]
            return as_path(value)

        return resolve_fd

    def resolve_fd(fd):
        value = os.readlink(f"/proc/self/fd/{fd}")
        if not value.startswith("/") or value.endswith(" (deleted)"):
            raise OSError("descriptor does not resolve to a current filesystem path")
        return as_path(value)

    return resolve_fd


def runtime_roots():
    values = set(sysconfig.get_paths().values())
    values.update([sys.base_prefix, sys.prefix, site.getusersitepackages()])
    try:
        values.update(site.getsitepackages())
    except AttributeError:
        pass
    return sorted({as_path(value) for value in values if value}, key=str)


def font_roots():
    values = ["/usr/share/fonts", "/usr/local/share/fonts", "/etc/fonts"]
    if os.name == "nt":
        values = [str(Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts")]
        local_app_data = os.environ.get("LOCALAPPDATA")
        if local_app_data:
            values.append(str(Path(local_app_data) / "Microsoft/Windows/Fonts"))
    return [as_path(value) for value in values]


def registered_font_files():
    if os.name != "nt":
        return set()
    import winreg

    files = set()
    key_name = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts"
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        try:
            with winreg.OpenKey(hive, key_name) as key:
                for index in range(winreg.QueryInfoKey(key)[1]):
                    value = winreg.EnumValue(key, index)[1]
                    if isinstance(value, str) and Path(value).is_absolute():
                        files.add(as_path(value))
        except OSError:
            continue
    return files


def open_access(mode, flags):
    """Audit open flags are authoritative, including O_RDONLY == 0 and integer fds."""
    if type(flags) is not int or flags < 0:
        raise ValueError("open flags must be a nonnegative integer")
    access_mask = getattr(os, "O_ACCMODE", os.O_RDONLY | os.O_WRONLY | os.O_RDWR)
    access = flags & access_mask
    if access not in (os.O_RDONLY, os.O_WRONLY, os.O_RDWR):
        raise ValueError("unsupported open access bits")
    reads = access in (os.O_RDONLY, os.O_RDWR)
    writes = access in (os.O_WRONLY, os.O_RDWR)
    mutating = os.O_CREAT | os.O_TRUNC | os.O_APPEND
    writes = writes or bool(flags & mutating)
    if mode is not None:
        if not isinstance(mode, str) or not mode:
            raise ValueError("open mode must be a nonempty string or None")
        if any(char not in "rwax+bt" for char in mode):
            raise ValueError("unknown open mode")
        if sum(char in mode for char in "rwax") != 1:
            raise ValueError("ambiguous open mode")
        # Union is fail-closed for contradictory/custom audit events.
        reads = reads or "r" in mode or "+" in mode
        writes = writes or any(char in mode for char in "wax+")
    return reads, writes, bool(flags & os.O_TRUNC)


class Boundary:
    def __init__(self, root, sources, out_root, report, script):
        self.root = root
        self.sources = set(sources)
        self.out_root = out_root
        self.report = report
        self.script = script
        self.phase = "source_hash_before"
        self.events = []
        self.violations = []
        self.created = set()
        self.runtime = runtime_roots()
        self.fonts = font_roots()
        self.font_files = registered_font_files()
        self.resolve_fd = prepare_fd_resolver()
        self.source_ids = {(path.stat().st_dev, path.stat().st_ino) for path in sources}

    def record(self, event, decision, **details):
        item = {
            "index": len(self.events) + 1,
            "phase": self.phase,
            "event": event,
            "decision": decision,
            **details,
        }
        self.events.append(item)
        return item

    def reject(self, event, reason, **details):
        item = self.record(event, "DENIED", reason=reason, **details)
        self.violations.append(item.copy())
        raise BoundaryViolation(f"{event}: {reason}: {details}")

    def path(self, value):
        if type(value) is int:
            return self.resolve_fd(value)
        if isinstance(value, (str, bytes, os.PathLike)):
            return as_path(value)
        raise ValueError("file path is neither path-like nor an integer descriptor")

    def readable(self, path):
        if path in self.sources:
            return "explicit_source"
        if path in self.created and inside(path, self.out_root):
            return "created_this_run"
        if path.suffix.lower() in CODE_SUFFIXES:
            return "code_or_import"
        # A project directory named venv/deps never grants data access.
        if inside(path, self.root):
            if any(inside(path, runtime) for runtime in self.runtime):
                if path.suffix.lower() in RUNTIME_DATA_SUFFIXES:
                    return "verified_runtime_asset"
                if path.name in RUNTIME_METADATA_NAMES and any(
                    part.endswith(".dist-info") for part in path.parts
                ):
                    return "verified_runtime_metadata"
            return None
        if any(inside(path, runtime) for runtime in self.runtime):
            return "external_runtime_dependency"
        if any(inside(path, font) for font in self.fonts):
            return "external_font_dependency"
        if path in self.font_files:
            return "registered_external_font"
        if path == Path(os.devnull).resolve():
            return "null_device"
        return None

    def writable(self, path):
        if not inside(path, self.out_root):
            return False
        if path in self.sources or path == self.script:
            return False
        if path.exists():
            info = path.stat()
            if (info.st_dev, info.st_ino) in self.source_ids:
                return False
        return True

    def on_open(self, args):
        try:
            if (
                args[1] is None
                and type(args[0]) is not int
                and not Path(os.fsdecode(args[0])).is_absolute()
            ):
                # open audit events omit os.open's dir_fd. Relative paths cannot
                # safely be interpreted as cwd-relative; require an absolute path.
                self.reject(
                    "open", "relative os.open path has ambiguous dir_fd", raw_args=repr(args)
                )
            path = self.path(args[0])
            reads, writes, truncates = open_access(args[1], args[2])
        except BoundaryViolation:
            raise
        except (OSError, ValueError, TypeError, IndexError) as error:
            self.reject(
                "open", "unresolved open path or mode", detail=str(error), raw_args=repr(args)
            )
        details = {
            "path": str(path),
            "mode": args[1],
            "flags": args[2],
            "reads": reads,
            "writes": writes,
            "inside_project": inside(path, self.root),
        }
        if writes and not self.writable(path):
            self.reject("open", "writes are restricted to this run's output directory", **details)
        existed = path.exists()
        if reads and not writes and not existed:
            self.record("open", "NO_READ", reason="missing path probe", **details)
            # Raise here so a file appearing after our existence check cannot be read.
            raise FileNotFoundError(2, "audit: read path does not exist", str(path))
        # w+/O_TRUNC may read only after destroying old content. r+/append do not.
        read_basis = self.readable(path)
        can_initialize = writes and (not existed or truncates) and inside(path, self.out_root)
        if reads and read_basis is None and not can_initialize:
            self.reject("open", "data read is not explicitly allowlisted", **details)
        if writes and existed and path not in self.created and not truncates:
            self.reject(
                "open",
                "existing output cannot be appended or updated before initialization",
                **details,
            )
        if writes and can_initialize:
            self.created.add(path)
        if (
            inside(path, self.root)
            or reads
            and read_basis == "explicit_source"
            or writes
            or read_basis is None
        ):
            self.record("open", "ALLOWED", basis=read_basis or "initialize_output", **details)

    def write_event(self, event, args):
        positions = {
            "os.remove": (0,),
            "os.rmdir": (0,),
            "os.mkdir": (0,),
            "os.chmod": (0,),
            "os.chown": (0,),
            "os.utime": (0,),
            "os.truncate": (0,),
            "os.rename": (0, 1),
        }[event]
        # Relative dir_fd operations must resolve against that fd, not cwd.
        fd_positions = {
            "os.remove": {0: 1},
            "os.rmdir": {0: 1},
            "os.mkdir": {0: 2},
            "os.chmod": {0: 2},
            "os.chown": {0: 3},
            "os.utime": {0: 3},
            "os.rename": {0: 2, 1: 3},
        }
        paths = []
        for position in positions:
            value = args[position]
            fd_position = fd_positions.get(event, {}).get(position)
            dir_fd = (
                args[fd_position] if fd_position is not None and len(args) > fd_position else -1
            )
            try:
                if type(value) is not int and not Path(os.fsdecode(value)).is_absolute():
                    if dir_fd not in (-1, None):
                        value = self.resolve_fd(dir_fd) / os.fsdecode(value)
                path = self.path(value)
            except (OSError, ValueError, TypeError) as error:
                self.reject(
                    event, "unresolved mutation path", detail=str(error), raw_args=repr(args)
                )
            paths.append(path)
            if not self.writable(path):
                self.reject(event, "filesystem mutation is outside output boundary", path=str(path))
        if event == "os.rename":
            if paths[0] not in self.created:
                self.reject(
                    event, "cannot move an untracked preexisting output", path=str(paths[0])
                )
            self.created.discard(paths[0])
            self.created.add(paths[1])
        self.record(event, "ALLOWED", paths=[str(path) for path in paths])

    def hook(self, event, args):
        if event == "open":
            self.on_open(args)
        elif event in {
            "os.remove",
            "os.rmdir",
            "os.mkdir",
            "os.rename",
            "os.chmod",
            "os.chown",
            "os.utime",
            "os.truncate",
        }:
            self.write_event(event, args)
        elif event in {
            "os.link",
            "os.symlink",
            "os.system",
            "os.exec",
            "os.posix_spawn",
            "os.spawn",
            "os.fork",
            "os.forkpty",
            "subprocess.Popen",
        }:
            self.reject(
                event, "links and child-process execution are outside this audit", args=repr(args)
            )
        elif event in {"os.listdir", "os.scandir"}:
            try:
                path = self.path(args[0] if args and args[0] is not None else ".")
            except (OSError, ValueError, TypeError):
                self.reject(event, "unresolved directory enumeration", args=repr(args))
            if inside(path, self.root):
                self.record(event, "METADATA_ONLY", path=str(path))


def file_hash(path):
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def validate(args):
    root, script, out_root, report = (
        as_path(value) for value in (args.project_root, args.script, args.out_root, args.report)
    )
    if not root.is_dir() or not script.is_file() or script.suffix != ".py":
        raise ValueError("project root and Python target script must exist")
    if not inside(script, root):
        raise ValueError("target script must be inside the project root")
    if out_root == root or not inside(out_root, root):
        raise ValueError("output directory must be a strict descendant of project root")
    if not inside(report, out_root) or report == out_root:
        raise ValueError("report must be a file inside the output directory")
    sources = [as_path(value) for value in args.source]
    if not sources or len(sources) != len(set(sources)):
        raise ValueError("sources must be explicitly supplied without duplicates")
    for source in sources:
        if not source.is_file() or source.suffix.lower() not in {".dxf", ".json"}:
            raise ValueError(f"source must be an existing DXF or declared semantic JSON: {source}")
        if inside(source, out_root):
            raise ValueError("a source cannot be inside the output directory")
        relative_parts = source.relative_to(root).parts if inside(source, root) else source.parts
        if any(part.lower() in FORBIDDEN_SOURCE_PARTS for part in relative_parts):
            raise ValueError(f"old outputs/gold cannot be allowlisted as replay sources: {source}")
        if source.suffix.lower() == ".json" and any(
            word in source.stem.lower() for word in ("gold", "frozen", "prediction")
        ):
            raise ValueError(f"gold/prediction JSON cannot be allowlisted: {source}")
    if script == report or script in sources or inside(script, out_root):
        raise ValueError("script, sources, and output data must be separate")
    return root, script, sources, out_root, report


def run(args):
    root, script, sources, out_root, report = validate(args)
    out_root.mkdir(parents=True, exist_ok=True)
    report.parent.mkdir(parents=True, exist_ok=True)
    cache = out_root / ".audit-cache"
    cache.mkdir(exist_ok=True)
    temporary = cache / "tmp"
    temporary.mkdir(exist_ok=True)
    os.environ.update(
        {
            "MPLCONFIGDIR": str(cache / "matplotlib"),
            "XDG_CACHE_HOME": str(cache),
            "XDG_CONFIG_HOME": str(cache / "config"),
            "EZDXF_CONFIG_FILE": "",
            "TMP": str(temporary),
            "TEMP": str(temporary),
            "TMPDIR": str(temporary),
        }
    )
    tempfile.tempdir = str(temporary)
    sys.dont_write_bytecode = True
    boundary = Boundary(root, sources, out_root, report, script)
    sys.addaudithook(boundary.hook)
    before = {str(source): file_hash(source) for source in sources}
    target_args = list(args.target_args)
    if target_args and target_args[0] == "--":
        target_args.pop(0)
    target_error = None
    target_exit = 0
    old_argv, old_path, old_cwd = sys.argv, sys.path.copy(), Path.cwd()
    boundary.phase = "target"
    try:
        os.chdir(root)
        sys.argv = [str(script), *target_args]
        sys.path.insert(0, str(script.parent))
        runpy.run_path(str(script), run_name="__main__")
    except SystemExit as error:
        target_exit = error.code if type(error.code) is int else (0 if error.code is None else 1)
        if target_exit:
            target_error = {"type": "SystemExit", "message": str(error.code)}
    except BaseException as error:
        target_exit = 1
        target_error = {
            "type": type(error).__name__,
            "message": str(error),
            "traceback": traceback.format_exc(),
        }
    finally:
        sys.argv, sys.path = old_argv, old_path
        os.chdir(old_cwd)
    boundary.phase = "source_hash_after"
    after = {}
    hash_errors = []
    for source in sources:
        try:
            after[str(source)] = file_hash(source)
        except BaseException as error:
            hash_errors.append(
                {"path": str(source), "type": type(error).__name__, "error": str(error)}
            )
    source_hashes = [
        {
            "path": str(source),
            "before": before[str(source)],
            "after": after.get(str(source)),
            "unchanged": before[str(source)] == after.get(str(source)),
        }
        for source in sources
    ]
    passed = (
        target_exit == 0
        and not boundary.violations
        and not hash_errors
        and all(item["unchanged"] for item in source_hashes)
    )
    boundary.phase = "audit_report"
    receipt = {
        "schema_version": "python-replay-read-boundary/1.0",
        "created_at": timestamp(),
        "status": "AUDITED_PASS" if passed else "AUDITED_FAIL",
        "project_root": str(root),
        "script": str(script),
        "argv": target_args,
        "out_root": str(out_root),
        "target_exit_code": target_exit,
        "target_error": target_error,
        "source_hashes": source_hashes,
        "hash_errors": hash_errors,
        "project_data_reads": [
            event
            for event in boundary.events
            if event["event"] == "open"
            and event["phase"] == "target"
            and event.get("reads")
            and event["decision"] == "ALLOWED"
            and event.get("basis") != "code_or_import"
        ],
        "open_events_are_attempts_not_byte_counts": True,
        "events": boundary.events.copy(),
        "violations": boundary.violations.copy(),
        "events_truncated": False,
        "runtime_roots": [str(path) for path in boundary.runtime],
        "limitations": LIMITATIONS,
        "is_blind": False,
        "accuracy_claim": None,
    }
    report.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "status": receipt["status"],
                "report": str(report),
                "target_exit_code": target_exit,
                "violations": receipt["violations"],
                "target_error": target_error,
                "source_hashes": source_hashes,
                "limitations": LIMITATIONS,
            },
            ensure_ascii=False,
        )
    )
    return 0 if passed else 2


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", required=True)
    parser.add_argument("--script", required=True)
    parser.add_argument("--source", action="append", required=True)
    parser.add_argument("--report", required=True)
    parser.add_argument("--out-root", required=True)
    parser.add_argument("target_args", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    try:
        return run(args)
    except BaseException as error:
        failure = {
            "status": "AUDIT_SETUP_FAILED",
            "type": type(error).__name__,
            "error": str(error),
            "traceback": traceback.format_exc(),
            "limitations": LIMITATIONS,
            "is_blind": False,
        }
        # Invalid setup cannot authorize an out-of-boundary report write.
        print(json.dumps(failure, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
