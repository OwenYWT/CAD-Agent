"""One generated CAD program, executed in its own killable process.

Mirrors backend/sandbox/executor_entry.py: same show_object capture, same
`result` fallback, same auto-fuse of stray solids, same STEP+STL export per
shown object, same result.json contract. Run as:

    python exec_child.py <work_dir> <mode>

with the source on stdin. A separate process is what makes the timeout real:
generated CAD can hang forever inside an OCP boolean, and only a process can be
killed for it -- which is precisely why the product runs this in a container.
"""
from __future__ import annotations

import json
import math
import sys
import traceback
from pathlib import Path


def main() -> None:
    work_dir = Path(sys.argv[1])
    mode = sys.argv[2] if len(sys.argv) > 2 else "3d"
    output_dir = work_dir / "output"
    input_dir = work_dir / "input"
    output_dir.mkdir(parents=True, exist_ok=True)
    input_dir.mkdir(parents=True, exist_ok=True)
    code = sys.stdin.read()

    try:
        files = run(code, mode, output_dir, input_dir)
        payload = {
            "status": "success",
            "files": {name: str(path) for name, path in files.items()},
        }
    except BaseException as exc:  # noqa: BLE001 - mirrors the sandbox entry
        payload = {
            "status": "error",
            "error_type": type(exc).__name__,
            "error_message": str(exc)[:2000],
            "traceback": traceback.format_exc()[:6000],
        }
    (output_dir / "result.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )


def run(code: str, mode: str, output_dir: Path, input_dir: Path) -> dict:
    import numpy as np
    import cadquery as cq

    try:
        import build123d as b3d
    except Exception:
        b3d = None

    shown: dict = {}

    def show_object(obj, name="result", **_kwargs):
        shown[name] = obj

    namespace: dict = {
        "cq": cq,
        "cadquery": cq,
        "math": math,
        "np": np,
        "numpy": np,
        "show_object": show_object,
    }
    if b3d is not None:
        namespace["b3d"] = b3d
        namespace["build123d"] = b3d
    try:
        import ezdxf

        namespace["ezdxf"] = ezdxf
    except Exception:
        pass

    out = str(output_dir).replace("\\", "/")
    src = code.replace("/sandbox/output", out).replace(
        "/sandbox/input", str(input_dir).replace("\\", "/")
    )
    exec(compile(src, "<generated>", "exec"), namespace)

    files: dict = {}
    if mode == "2d":
        candidates = sorted(output_dir.glob("*.dxf"))
        if not candidates:
            raise ValueError(
                "2D mode: expected a DXF output but no .dxf file was created."
            )
        files["result.dxf"] = candidates[0]
        return files

    if not shown and "result" in namespace:
        shown["result"] = namespace["result"]
    if not shown:
        raise ValueError(
            "No result found. Code must call show_object(result) or define a "
            "'result' variable."
        )

    for name, obj in shown.items():
        step_path = output_dir / f"{name}.step"
        stl_path = output_dir / f"{name}.stl"

        if b3d is not None and isinstance(obj, getattr(b3d, "Shape", ())):
            b3d.export_step(obj, str(step_path))
            files[f"{name}.step"] = step_path
            b3d.export_stl(obj, str(stl_path))
            files[f"{name}.stl"] = stl_path
            continue

        if isinstance(obj, cq.Assembly):
            obj.save(str(step_path))
            files[f"{name}.step"] = step_path
            try:
                compound = obj.toCompound()
                cq.exporters.export(
                    cq.Workplane().add(compound), str(stl_path), exportType="STL"
                )
                files[f"{name}.stl"] = stl_path
            except Exception:
                pass
            continue

        try:
            solids = obj.val().Solids() if hasattr(obj.val(), "Solids") else []
            if len(solids) > 1:
                fused = solids[0]
                for extra in solids[1:]:
                    fused = fused.fuse(extra)
                obj = cq.Workplane().add(cq.Shape(fused.wrapped))
        except Exception:
            pass

        cq.exporters.export(obj, str(step_path), exportType="STEP")
        files[f"{name}.step"] = step_path
        cq.exporters.export(obj, str(stl_path), exportType="STL")
        files[f"{name}.stl"] = stl_path

    return files


if __name__ == "__main__":
    main()
