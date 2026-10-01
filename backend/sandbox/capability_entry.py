"""Allowlisted MCAD capability dispatcher running inside the worker sandbox."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import traceback
from pathlib import Path
from typing import Any, Sequence
from uuid import uuid4

try:
    from freecad_result_channel import ResultProtocolError, read_result
except ModuleNotFoundError:
    from sandbox.freecad_result_channel import ResultProtocolError, read_result


INPUT_ROOT = Path("/sandbox/input")
OUTPUT_ROOT = Path("/sandbox/output")
SKILLS_ROOT = Path("/opt/cadskills/skills")
RESULT_PATH = OUTPUT_ROOT / "result.json"
METADATA_PATH = OUTPUT_ROOT / "capability-result.json"
SUPPORTED = {
    ("cad", "step"),
    ("cad", "generate"),
    ("cad", "export"),
    ("cad", "inspect"),
    ("cad", "snapshot"),
    ("geometry", "validate"),
    ("visual", "render"),
    ("dfm", "validate"),
    ("dxf", "generate"),
    ("implicit-cad", "export"),
    ("implicit-cad", "snapshot"),
    ("freecad", "execute"),
    ("freecad", "bom"),
    ("freecad", "scene"),
    ("freecad", "engineering"),
}


class StructuredCapabilityError(RuntimeError):
    def __init__(self, error: dict[str, Any]) -> None:
        super().__init__(str(error["message"]))
        self.error = error


def _freecad_error_envelope(raw: object) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("FreeCAD runner returned no structured error")
    code = raw.get("code")
    message = raw.get("message")
    op_id = raw.get("op_id")
    action = raw.get("action")
    details = raw.get("details", {})
    if (
        not isinstance(code, str)
        or not 1 <= len(code) <= 200
        or not isinstance(message, str)
        or not 1 <= len(message) <= 4000
        or (op_id is not None and not isinstance(op_id, str))
        or (action is not None and not isinstance(action, str))
        or not isinstance(details, dict)
    ):
        raise ValueError("FreeCAD runner returned a malformed structured error")
    return {
        "schema_version": "mcad-error.v1",
        "code": code,
        "message": message,
        "operation_id": op_id,
        "action": action,
        "details": details,
    }


def _safe_name(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 255:
        raise ValueError(f"{label} must be a non-empty filename")
    if (
        Path(value).name != value
        or value.startswith(("-", "."))
        or "/" in value
        or "\\" in value
    ):
        raise ValueError(f"{label} must be a safe basename")
    return value


def _input(task: dict[str, Any], role: str) -> Path:
    inputs = task["inputs"]
    name = _safe_name(inputs.get(role), f"input {role}")
    path = INPUT_ROOT / name
    if not path.is_file() or path.is_symlink():
        raise ValueError(f"declared input is not a regular file: {role}")
    return path


def _output(params: dict[str, Any]) -> Path:
    name = _safe_name(params.get("output"), "output")
    path = OUTPUT_ROOT / name
    if path.parent != OUTPUT_ROOT:
        raise ValueError("output escaped the sandbox output root")
    return path


def _run(
    command: Sequence[str],
    *,
    cwd: Path,
    timeout: int = 300,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    completed = subprocess.run(
        [str(part) for part in command],
        cwd=cwd,
        env=env,
        shell=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout,
        check=False,
    )
    stdout = completed.stdout[:256 * 1024]
    stderr = completed.stderr[:256 * 1024]
    if completed.returncode != 0:
        detail = (stderr or stdout or f"command exited with {completed.returncode}").strip()
        raise RuntimeError(detail[-4000:])
    try:
        parsed: Any = json.loads(stdout) if stdout.strip() else None
    except json.JSONDecodeError:
        parsed = stdout
    return {
        "exit_code": completed.returncode,
        "result": parsed,
        "stderr": stderr or None,
        "stdout_truncated": len(completed.stdout) > len(stdout),
        "stderr_truncated": len(completed.stderr) > len(stderr),
    }


def _freecad(task: dict[str, Any]) -> tuple[dict[str, Path], dict[str, Any]]:
    invocation = uuid4().hex
    result_path = OUTPUT_ROOT / f'.freecad-result-{invocation}.json'
    env = {
        **os.environ,
        "HOME": "/tmp",
        "TMPDIR": "/tmp",
        "LANG": "C.UTF-8",
        "PYTHONPATH": "/opt/cad-agent",
        "CAD_FREECAD_INVOCATION_ID": invocation,
        "CAD_FREECAD_RESULT_PATH": str(result_path),
    }
    completed = subprocess.run(
        [
            "/opt/freecad/bin/FreeCADCmd",
            "-P",
            "/opt/cad-agent",
            "-c",
            (
                "exec(compile(open('/opt/cad-agent/freecad_entry.py', "
                "encoding='utf-8').read(), "
                "'/opt/cad-agent/freecad_entry.py', 'exec'))"
            ),
        ],
        cwd=OUTPUT_ROOT,
        env=env,
        shell=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=None,
        check=False,
    )
    stdout = completed.stdout[:512 * 1024]
    stderr = completed.stderr[:256 * 1024]
    try:
        parsed = read_result(result_path, invocation, success_schema=(
            'freecad-engineering-result.v1' if task.get('operation') == 'engineering'
            else 'freecad-operation-result.v1'
        ))
        if completed.returncode != 0 and parsed['status'] == 'succeeded':
            raise ResultProtocolError('FreeCAD exited abnormally after reporting success')
    except ResultProtocolError as exc:
        raise StructuredCapabilityError(_freecad_error_envelope({
            'code':'sandbox_protocol_error', 'message':str(exc)[:4000],
            'details':{'exit_code':completed.returncode, 'stderr':stderr[-4000:]},
        })) from exc
    finally:
        result_path.unlink(missing_ok=True)
    if parsed.get("status") != "succeeded":
        raise StructuredCapabilityError(
            _freecad_error_envelope(parsed.get("error"))
        )
    files = parsed.get("files")
    if not isinstance(files, dict) or not files:
        raise RuntimeError("FreeCAD runner returned no artifact files")
    artifacts: dict[str, Path] = {}
    for role, raw_path in files.items():
        _safe_name(role, "FreeCAD artifact role")
        path = Path(str(raw_path))
        if path.parent != OUTPUT_ROOT:
            raise RuntimeError(f"FreeCAD artifact escaped output root: {role}")
        artifacts[role] = path
    return artifacts, {
        "exit_code": completed.returncode,
        "result": parsed,
        "stderr": stderr or None,
        "stdout_truncated": len(completed.stdout) > len(stdout),
        "stderr_truncated": len(completed.stderr) > len(stderr),
    }


def _timestamped(path: Path) -> Path:
    if path.is_file():
        return path
    candidates = sorted(path.parent.glob(f"{path.stem}_*{path.suffix}"))
    if not candidates:
        raise RuntimeError(f"expected output was not produced: {path.name}")
    return candidates[-1]


def _cad_step(task: dict[str, Any]) -> tuple[Path, dict[str, Any]]:
    params = task["params"]
    source = _input(task, "input")
    output = _output(params)
    cli = SKILLS_ROOT / "cad" / "scripts" / "step" / "__main__.py"
    if source.suffix.lower() == ".py":
        command = [sys.executable, str(cli), str(source), "--output", str(output)]
        cwd = OUTPUT_ROOT
    else:
        shutil.copy2(source, output)
        command = [
            sys.executable,
            str(cli),
            output.name,
            "--kind",
            str(params.get("kind", "part")),
        ]
        cwd = OUTPUT_ROOT
    if params.get("force"):
        command.append("--force")
    for key, flag in (
        ("mesh_tolerance", "--mesh-tolerance"),
        ("mesh_angular_tolerance", "--mesh-angular-tolerance"),
    ):
        if key in params:
            command.extend([flag, str(params[key])])
    metadata = _run(command, cwd=cwd)
    return output, metadata


def _cad_export(task: dict[str, Any]) -> tuple[Path, dict[str, Any]]:
    params = task["params"]
    source = _input(task, "input")
    local_source = OUTPUT_ROOT / source.name
    shutil.copy2(source, local_source)
    output = _output(params)
    export_format = str(params["format"])
    if export_format == "stl":
        import cadquery as cq

        model = cq.importers.importStep(str(local_source))
        if not model.val().Solids():
            raise RuntimeError("STEP import produced no solids for STL export")
        cq.exporters.export(model, str(output), exportType="STL")
        return output, {
            "exit_code": 0,
            "result": {
                "format": "stl",
                "source": source.name,
                "output": output.name,
                "exporter": "cadquery-ocp",
            },
            "stderr": None,
            "stdout_truncated": False,
            "stderr_truncated": False,
        }
    flag = {"stl": "--stl", "3mf": "--3mf", "glb": "--glb"}[export_format]
    command = [
        sys.executable,
        str(SKILLS_ROOT / "cad" / "scripts" / "step" / "__main__.py"),
        local_source.name,
        "--kind",
        str(params.get("kind", "part")),
        flag,
        output.name,
    ]
    if params.get("force"):
        command.append("--force")
    for key, option in (
        ("mesh_tolerance", "--mesh-tolerance"),
        ("mesh_angular_tolerance", "--mesh-angular-tolerance"),
    ):
        if key in params:
            command.extend([option, str(params[key])])
    metadata = _run(command, cwd=OUTPUT_ROOT)
    return output, metadata


def _cad_inspect(task: dict[str, Any]) -> tuple[Path, dict[str, Any]]:
    params = task["params"]
    operation = str(params.get("operation", "refs"))
    entry = _input(task, "input")
    local_entry = OUTPUT_ROOT / entry.name
    shutil.copy2(entry, local_entry)
    command = [
        sys.executable,
        str(SKILLS_ROOT / "cad" / "scripts" / "inspect" / "__main__.py"),
        operation,
    ]
    if operation == "refs":
        command.append(local_entry.name)
        command.extend(str(value) for value in params.get("selectors", []))
        for key, flag in (
            ("detail", "--detail"),
            ("facts", "--facts"),
            ("positioning", "--positioning"),
            ("planes", "--planes"),
            ("topology", "--topology"),
        ):
            if params.get(key):
                command.append(flag)
    elif operation == "diff":
        right = _input(task, "right")
        local_right = OUTPUT_ROOT / right.name
        shutil.copy2(right, local_right)
        command.extend([local_entry.name, local_right.name])
        if params.get("planes"):
            command.append("--planes")
    elif operation == "frame":
        command.append(local_entry.name)
        if params.get("selector"):
            command.append(str(params["selector"]))
    elif operation == "measure":
        command.extend(
            [
                local_entry.name,
                "--from",
                str(params["from_selector"]),
                "--to",
                str(params["to_selector"]),
            ]
        )
        if params.get("axis"):
            command.extend(["--axis", str(params["axis"])])
    elif operation == "align":
        command.extend(
            [
                local_entry.name,
                "--moving",
                str(params["moving"]),
                "--target",
                str(params["target"]),
                "--mode",
                str(params.get("mode", "flush")),
            ]
        )
        if params.get("axis"):
            command.extend(["--axis", str(params["axis"])])
        if "offset" in params:
            command.extend(["--offset", str(params["offset"])])
    else:
        raise ValueError("unsupported CAD inspect operation")
    command.extend(["--format", "json", "--quiet"])
    metadata = _run(command, cwd=OUTPUT_ROOT)
    output = _output(params)
    output.write_text(
        json.dumps(metadata["result"], ensure_ascii=False),
        encoding="utf-8",
    )
    return output, metadata


def _cad_snapshot(task: dict[str, Any]) -> tuple[Path, dict[str, Any]]:
    params = task["params"]
    source = _input(task, "input")
    local_source = OUTPUT_ROOT / source.name
    shutil.copy2(source, local_source)
    output = _output(params)
    command = [
        sys.executable,
        str(SKILLS_ROOT / "cad" / "scripts" / "snapshot" / "__main__.py"),
        "--input",
        local_source.name,
        "--output",
        output.name,
        "--mode",
        str(params.get("mode", "view")),
        "--json",
    ]
    for key in ("camera", "width", "height", "size_profile"):
        if key in params:
            command.extend([f"--{key.replace('_', '-')}", str(params[key])])
    for key in ("focus", "hide"):
        for value in params.get(key, []):
            command.extend([f"--{key}", str(value)])
    metadata = _run(command, cwd=OUTPUT_ROOT)
    return _timestamped(output), metadata


def _dxf_generate(task: dict[str, Any]) -> tuple[Path, dict[str, Any]]:
    params = task["params"]
    source = _input(task, "source")
    output = _output(params)
    command = [
        sys.executable,
        str(SKILLS_ROOT / "dxf" / "scripts" / "dxf" / "__main__.py"),
        str(source),
        "--output",
        str(output),
    ]
    if params.get("verbose"):
        command.append("--verbose")
    return output, _run(command, cwd=OUTPUT_ROOT)


def _implicit(task: dict[str, Any]) -> tuple[Path, dict[str, Any]]:
    params = task["params"]
    source = _input(task, "input")
    output = _output(params)
    operation = task["operation"]
    script = (
        SKILLS_ROOT
        / "implicit-cad"
        / "scripts"
        / ("export.mjs" if operation == "export" else "snapshot.mjs")
    )
    command = [
        "node",
        str(script),
        "--input",
        str(source),
        "--output",
        str(output),
    ]
    if operation == "export":
        command.extend(["--format", str(params["format"])])
        for key in ("resolution", "max_cells"):
            if key in params:
                command.extend([f"--{key.replace('_', '-')}", str(params[key])])
    else:
        command.extend(["--mode", str(params.get("mode", "view"))])
        for key in ("camera", "width", "height"):
            if key in params:
                command.extend([f"--{key}", str(params[key])])
    if "parameters" in params:
        command.extend(
            [
                "--params",
                json.dumps(params["parameters"], separators=(",", ":")),
            ]
        )
    command.append("--json")
    metadata = _run(command, cwd=OUTPUT_ROOT)
    return (_timestamped(output) if operation == "snapshot" else output), metadata


def _geometry_validate(task: dict[str, Any]) -> tuple[Path, dict[str, Any]]:
    from geometry_validation import validate_geometry_files

    params = task["params"]
    artifacts = []
    for item in params.get("artifacts") or []:
        role = str(item["role"])
        source = _input(task, role)
        artifacts.append(
            {
                "role": role,
                "format": str(item["format"]),
                "path": str(source),
            }
        )
    if not artifacts:
        raise ValueError("geometry validation requires at least one artifact")
    report = validate_geometry_files(
        artifacts,
        expected_dimensions=dict(params.get("expected_dimensions_mm") or {}),
        dimension_tolerance=float(params.get("dimension_tolerance", 0.05)),
        expected_solid_count=params.get("expected_solid_count"),
        acceptance=params.get("acceptance"),
    )
    output = _output(params)
    output.write_text(
        json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    return output, {
        "exit_code": 0,
        "result": report,
        "stderr": None,
        "stdout_truncated": False,
        "stderr_truncated": False,
    }


def _visual_render(task: dict[str, Any]) -> tuple[dict[str, Path], dict[str, Any]]:
    from visual_render import render_four_views

    params = task["params"]
    source = _input(task, "model")
    outputs, report = render_four_views(
        source,
        OUTPUT_ROOT,
        width=int(params.get("width", 512)),
        height=int(params.get("height", 512)),
    )
    return outputs, {
        "exit_code": 0,
        "result": report,
        "stderr": None,
        "stdout_truncated": False,
        "stderr_truncated": False,
    }


def _dfm_validate(task: dict[str, Any]) -> tuple[Path, dict[str, Any]]:
    from dfm_validation import evaluate_dfm

    params = task["params"]
    report = evaluate_dfm(
        _input(task, "model"),
        _input(task, "policy"),
        expected_policy_hash=str(params["policy_hash"]),
    )
    output = _output(params)
    output.write_text(
        json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    return output, {
        "exit_code": 0,
        "result": report,
        "stderr": None,
        "stdout_truncated": False,
        "stderr_truncated": False,
    }


def run_task(task: dict[str, Any]) -> None:
    if task.get("schema_version") != "mcad-capability-task.v1":
        raise ValueError("unsupported MCAD capability task schema")
    pair = (task.get("capability"), task.get("operation"))
    if pair not in SUPPORTED:
        raise ValueError("unsupported MCAD capability task")
    if not isinstance(task.get("params"), dict) or not isinstance(task.get("inputs"), dict):
        raise ValueError("task params and inputs must be objects")

    capability, operation = pair
    if capability == "cad" and operation in {"step", "generate"}:
        artifact, metadata = _cad_step(task)
    elif capability == "cad" and operation == "export":
        artifact, metadata = _cad_export(task)
    elif capability == "cad" and operation == "inspect":
        artifact, metadata = _cad_inspect(task)
    elif capability == "cad" and operation == "snapshot":
        artifact, metadata = _cad_snapshot(task)
    elif capability == "geometry" and operation == "validate":
        artifact, metadata = _geometry_validate(task)
    elif capability == "visual" and operation == "render":
        artifact, metadata = _visual_render(task)
    elif capability == "dfm" and operation == "validate":
        artifact, metadata = _dfm_validate(task)
    elif capability == "dxf":
        artifact, metadata = _dxf_generate(task)
    elif capability == "freecad":
        artifact, metadata = _freecad(task)
    else:
        artifact, metadata = _implicit(task)

    artifacts = artifact if isinstance(artifact, dict) else {"artifact": artifact}
    for name, path in artifacts.items():
        _safe_name(str(name), "artifact role")
        if not path.is_file() or path.is_symlink() or path.stat().st_size <= 0:
            produced = sorted(
                candidate.name
                for candidate in OUTPUT_ROOT.iterdir()
                if candidate.is_file() and not candidate.is_symlink()
            )
            raise RuntimeError(
                "MCAD capability did not produce the declared non-empty artifact; "
                f"produced files: {produced}"
            )
    METADATA_PATH.write_text(
        json.dumps(metadata, ensure_ascii=False),
        encoding="utf-8",
    )
    RESULT_PATH.write_text(
        json.dumps(
            {
                "status": "success",
                "files": {
                    **{name: str(path) for name, path in artifacts.items()},
                    "capability-result": str(METADATA_PATH),
                },
            }
        ),
        encoding="utf-8",
    )


def main() -> int:
    try:
        task = json.loads((INPUT_ROOT / "task.json").read_text(encoding="utf-8"))
        if not isinstance(task, dict):
            raise ValueError("MCAD capability task must be an object")
        run_task(task)
        return 0
    except Exception as exc:
        if isinstance(exc, StructuredCapabilityError):
            payload = {
                "status": "error",
                "error": exc.error,
            }
        else:
            payload = {
                "status": "error",
                "error_type": type(exc).__name__,
                "error_message": str(exc)[:4000],
                "traceback": traceback.format_exc(limit=20),
            }
        RESULT_PATH.write_text(
            json.dumps(payload),
            encoding="utf-8",
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
