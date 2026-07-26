"""End-to-end probe for the immutable MCAD worker image.

The probe intentionally runs the same vendored CLIs that production capability
actions use. It creates and re-imports real artifacts; it never fabricates a
success result when an optional renderer or exporter is unavailable.
"""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Sequence


LOCK_PATH = Path("/opt/cad-agent/runtime-lock.json")
SKILLS_ROOT = Path("/opt/cadskills/skills")
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


class ProbeFailure(RuntimeError):
    pass


def _run(command: Sequence[str], *, cwd: Path, timeout: int = 300) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        [str(part) for part in command],
        cwd=cwd,
        env={
            **os.environ,
            "HOME": str(cwd),
            "MPLCONFIGDIR": str(cwd / ".mpl"),
            "XDG_CACHE_HOME": str(cwd / ".cache"),
            "NUMBA_CACHE_DIR": str(cwd / ".numba"),
            "TMPDIR": str(cwd),
        },
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise ProbeFailure(f"{' '.join(command)} failed: {detail[-4000:]}")
    return completed


def _require_file(path: Path, *, minimum_bytes: int = 32) -> Path:
    if not path.is_file():
        raise ProbeFailure(f"required artifact was not produced: {path}")
    if path.stat().st_size < minimum_bytes:
        raise ProbeFailure(f"artifact is unexpectedly small: {path}")
    return path


def _latest_timestamped(directory: Path, stem: str, suffix: str) -> Path:
    candidates = sorted(directory.glob(f"{stem}_*{suffix}"))
    if not candidates:
        raise ProbeFailure(f"timestamped artifact was not produced: {stem}_*{suffix}")
    return candidates[-1]


def _load_lock() -> dict[str, Any]:
    if not LOCK_PATH.is_file():
        raise ProbeFailure(f"runtime lock is missing: {LOCK_PATH}")
    return json.loads(LOCK_PATH.read_text(encoding="utf-8"))


def _verify_versions(lock: dict[str, Any]) -> dict[str, str]:
    if sys.version.split()[0] != lock["python"]["version"]:
        raise ProbeFailure(
            f"Python version mismatch: {sys.version.split()[0]} != {lock['python']['version']}"
        )

    versions = {"python": sys.version.split()[0]}
    for package, expected in lock["python"]["packages"].items():
        actual = importlib.metadata.version(package)
        versions[package] = actual
        if actual != expected:
            raise ProbeFailure(f"{package} version mismatch: {actual} != {expected}")

    node = _run(["node", "--version"], cwd=Path("/tmp"), timeout=15).stdout.strip().removeprefix("v")
    versions["node"] = node
    if node != lock["node"]["version"]:
        raise ProbeFailure(f"Node version mismatch: {node} != {lock['node']['version']}")

    implicit_package = (
        SKILLS_ROOT / "implicit-cad" / "scripts" / "packages" / "implicitjs"
    )
    for package, expected in lock["node"]["packages"].items():
        package_json = implicit_package / "node_modules" / package / "package.json"
        actual = json.loads(package_json.read_text(encoding="utf-8"))["version"]
        versions[f"node:{package}"] = actual
        if actual != expected:
            raise ProbeFailure(f"Node package {package} version mismatch: {actual} != {expected}")
    return versions


def _write_sources(work: Path) -> tuple[Path, Path, Path]:
    build123d_source = work / "probe_part.py"
    build123d_source.write_text(
        "from build123d import Box\n\n"
        "def gen_step():\n"
        "    return Box(12, 8, 4)\n",
        encoding="utf-8",
    )

    dxf_source = work / "probe_drawing.py"
    dxf_source.write_text(
        "import ezdxf\n\n"
        "def gen_dxf():\n"
        "    doc = ezdxf.new('R2010')\n"
        "    doc.units = ezdxf.units.MM\n"
        "    doc.modelspace().add_lwpolyline("
        "[(0, 0), (40, 0), (40, 20), (0, 20)], close=True)\n"
        "    return doc\n",
        encoding="utf-8",
    )

    implicit_source = work / "probe.implicit.mjs"
    implicit_source.write_text(
        "export default {\n"
        "  glsl: 'float sdf(vec3 p) { return length(p) - 1.0; }'\n"
        "};\n",
        encoding="utf-8",
    )
    return build123d_source, dxf_source, implicit_source


def _probe_python_cad(work: Path, build123d_source: Path, dxf_source: Path) -> dict[str, Path]:
    import cadquery as cq
    import ezdxf
    import trimesh
    from build123d import Box

    cadquery_shape = cq.Workplane("XY").box(12, 8, 4).faces(">Z").hole(2)
    if cadquery_shape.val().Volume() <= 0:
        raise ProbeFailure("CadQuery generated empty geometry")

    # Exercise build123d independently of the vendored CLI.
    build123d_shape = Box(12, 8, 4)
    if float(build123d_shape.volume) <= 0:
        raise ProbeFailure("build123d generated empty geometry")

    cadquery_step = work / "cadquery.step"
    cadquery_stl = work / "cadquery.stl"
    cadquery_svg = work / "cadquery.svg"
    cq.exporters.export(cadquery_shape, str(cadquery_step), exportType="STEP")
    cq.exporters.export(cadquery_shape, str(cadquery_stl), exportType="STL")
    cq.exporters.export(cadquery_shape, str(cadquery_svg), exportType="SVG")

    step_cli = SKILLS_ROOT / "cad" / "scripts" / "step" / "__main__.py"
    skill_step = work / "skill.step"
    _run(
        [sys.executable, str(step_cli), str(build123d_source), "--output", str(skill_step), "--force"],
        cwd=work,
    )

    inspect_cli = SKILLS_ROOT / "cad" / "scripts" / "inspect" / "__main__.py"
    inspection = _run(
        [
            sys.executable,
            str(inspect_cli),
            "refs",
            skill_step.name,
            "--format",
            "json",
            "--quiet",
        ],
        cwd=work,
    )
    inspect_json = work / "inspect.json"
    inspect_json.write_text(inspection.stdout, encoding="utf-8")

    dxf_cli = SKILLS_ROOT / "dxf" / "scripts" / "dxf" / "__main__.py"
    dxf_path = work / "probe.dxf"
    _run(
        [sys.executable, str(dxf_cli), str(dxf_source), "--output", str(dxf_path)],
        cwd=work,
    )

    snapshot_cli = SKILLS_ROOT / "cad" / "scripts" / "snapshot" / "__main__.py"
    _run(
        [
            sys.executable,
            str(snapshot_cli),
            "--input",
            str(skill_step),
            "--output",
            str(work / "cad_snapshot.png"),
            "--mode",
            "view",
            "--width",
            "320",
            "--height",
            "240",
            "--json",
        ],
        cwd=work,
    )
    cad_png = _latest_timestamped(work, "cad_snapshot", ".png")

    imported = cq.importers.importStep(str(cadquery_step))
    if not imported.val().Solids():
        raise ProbeFailure("CadQuery could not re-import its STEP artifact")
    stl_mesh = trimesh.load_mesh(cadquery_stl, force="mesh")
    if stl_mesh.is_empty or len(stl_mesh.faces) == 0:
        raise ProbeFailure("trimesh found no faces in the STL artifact")
    ezdxf.readfile(dxf_path)
    ET.parse(cadquery_svg)
    if _require_file(cad_png, minimum_bytes=100).read_bytes()[:8] != PNG_SIGNATURE:
        raise ProbeFailure("CAD snapshot is not a PNG")

    return {
        "step": _require_file(cadquery_step),
        "stl": _require_file(cadquery_stl),
        "svg": _require_file(cadquery_svg),
        "skill_step": _require_file(skill_step),
        "dxf": _require_file(dxf_path),
        "inspect": _require_file(inspect_json),
        "cad_snapshot": cad_png,
    }


def _probe_implicit_cad(work: Path, implicit_source: Path) -> dict[str, Path]:
    import trimesh

    implicit_root = SKILLS_ROOT / "implicit-cad" / "scripts"
    implicit_stl = work / "implicit.stl"
    _run(
        [
            "node",
            str(implicit_root / "export.mjs"),
            "--input",
            str(implicit_source),
            "--output",
            str(implicit_stl),
            "--format",
            "stl",
            "--resolution",
            "32",
            "--max-cells",
            "100000",
            "--json",
        ],
        cwd=work,
    )
    implicit_mesh = trimesh.load_mesh(implicit_stl, force="mesh")
    if implicit_mesh.is_empty or len(implicit_mesh.faces) == 0:
        raise ProbeFailure("implicit-CAD STL contains no faces")

    _run(
        [
            "node",
            str(implicit_root / "snapshot.mjs"),
            "--input",
            str(implicit_source),
            "--output",
            str(work / "implicit_snapshot.png"),
            "--mode",
            "view",
            "--width",
            "320",
            "--height",
            "240",
            "--json",
        ],
        cwd=work,
    )
    implicit_png = _latest_timestamped(work, "implicit_snapshot", ".png")
    if _require_file(implicit_png, minimum_bytes=100).read_bytes()[:8] != PNG_SIGNATURE:
        raise ProbeFailure("implicit-CAD snapshot is not a PNG")
    return {
        "implicit_stl": _require_file(implicit_stl),
        "implicit_snapshot": implicit_png,
    }


def run_probe(output_dir: Path) -> dict[str, Any]:
    lock = _load_lock()
    versions = _verify_versions(lock)
    output_dir.mkdir(parents=True, exist_ok=True)
    build123d_source, dxf_source, implicit_source = _write_sources(output_dir)
    artifacts = {
        **_probe_python_cad(output_dir, build123d_source, dxf_source),
        **_probe_implicit_cad(output_dir, implicit_source),
    }
    return {
        "status": "success",
        "schema_version": lock["schema_version"],
        "versions": versions,
        "artifacts": {
            name: {
                "path": str(path),
                "size_bytes": path.stat().st_size,
            }
            for name, path in artifacts.items()
        },
        "verified_operations": lock["verified_operations"],
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Probe the complete immutable MCAD runtime")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(list(argv) if argv is not None else None)

    temporary: tempfile.TemporaryDirectory[str] | None = None
    try:
        if args.output_dir is None:
            temporary = tempfile.TemporaryDirectory(prefix="mcad-runtime-probe-")
            output_dir = Path(temporary.name)
        else:
            output_dir = args.output_dir.expanduser().resolve()
            if output_dir.exists():
                shutil.rmtree(output_dir)
        result = run_probe(output_dir)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True) if args.json else result)
        return 0
    except Exception as exc:
        failure = {"status": "error", "error_type": type(exc).__name__, "message": str(exc)}
        print(json.dumps(failure, ensure_ascii=False, sort_keys=True), file=sys.stderr)
        return 1
    finally:
        if temporary is not None:
            temporary.cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
