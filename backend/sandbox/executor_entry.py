"""
Sandbox executor entry point.
Runs inside Docker container with restricted globals.
Reads /sandbox/input/input.py, executes it, exports results to /sandbox/output/.
"""
import builtins
import json
import os
import sys
import traceback

# Capture the real import before any restriction
_real_import = builtins.__import__

# Ensure output directory exists
os.makedirs("/sandbox/output", exist_ok=True)


def main():
    try:
        task_path = "/sandbox/input/task.json"
        if os.path.exists(task_path):
            from capability_entry import main as capability_main

            raise SystemExit(capability_main())

        # Read input code
        with open("/sandbox/input/input.py", "r") as f:
            code = f.read()

        # Read execution mode: "2d", "3d" (default), or "analysis"
        exec_mode = "3d"
        mode_path = "/sandbox/input/mode.txt"
        if os.path.exists(mode_path):
            exec_mode = open(mode_path).read().strip().lower()

        import math
        import numpy as np

        cad_modules = {
            "build123d": "build123d", "b3d": "build123d",
            "cadquery": "cadquery", "cq": "cadquery",
        }
        _PRE_IMPORTED = {
            "math": math,
            "numpy": np,
            "np": np,
        }

        # Try to import ezdxf if available (for 2D DXF generation)
        try:
            import ezdxf as _ezdxf
            _PRE_IMPORTED["ezdxf"] = _ezdxf
        except ImportError:
            pass

        _ALLOWED_ROOTS = _PRE_IMPORTED.keys() | cad_modules.keys() | {"OCP"}
        _DENIED_MODULES = frozenset({
            "os", "sys", "subprocess", "pathlib", "builtins", "importlib",
            "inspect", "socket", "ctypes", "shutil", "signal", "pickle",
            "shelve", "code", "codeop", "compileall", "py_compile",
            "multiprocessing", "threading", "http", "urllib", "requests",
            "io", "tempfile", "glob", "fnmatch", "webbrowser",
        })

        def _safe_import(name, globals=None, locals=None, fromlist=(), level=0):
            """Controlled import that only permits whitelisted modules."""
            root = name.split(".")[0]
            if root in _DENIED_MODULES:
                raise ImportError(f"Import of '{name}' is not allowed in sandbox")
            if root not in _ALLOWED_ROOTS:
                raise ImportError(f"Import of '{name}' is not allowed in sandbox")
            # For submodule imports (e.g., OCP.BRepAdaptor), use real import
            if "." in name or fromlist:
                return _real_import(name, globals, locals, fromlist, level)
            # For simple top-level imports, return pre-imported if available
            if root in _PRE_IMPORTED:
                return _PRE_IMPORTED[root]
            return _real_import(cad_modules.get(name, name), globals, locals, fromlist, level)

        class SandboxGlobals(dict):
            # Preserve prebound CAD names in user functions without loading
            # either kernel into tasks that only need NumPy or DXF.
            def __missing__(self, name):
                if name not in cad_modules:
                    raise KeyError(name)
                module = _safe_import(name)
                self[name] = module
                return module

        # Restricted builtins — no __import__, eval, exec, compile, open
        safe_builtins = {
            "range": range,
            "len": len,
            "int": int,
            "float": float,
            "str": str,
            "list": list,
            "dict": dict,
            "tuple": tuple,
            "set": set,
            "frozenset": frozenset,
            "bool": bool,
            "True": True,
            "False": False,
            "None": None,
            "abs": abs,
            "min": min,
            "max": max,
            "round": round,
            "sum": sum,
            "enumerate": enumerate,
            "zip": zip,
            "map": map,
            "filter": filter,
            "sorted": sorted,
            "reversed": reversed,
            "print": print,
            "isinstance": isinstance,
            "issubclass": issubclass,
            "hasattr": hasattr,
            "getattr": getattr,
            "type": type,
            "ValueError": ValueError,
            "TypeError": TypeError,
            "RuntimeError": RuntimeError,
            "Exception": Exception,
            "KeyError": KeyError,
            "IndexError": IndexError,
            "AttributeError": AttributeError,
            "ZeroDivisionError": ZeroDivisionError,
            "__import__": _safe_import,
        }

        # Storage for show_object calls
        shown_objects = {}

        def show_object(obj, name="result", **kwargs):
            shown_objects[name] = obj

        safe_globals = SandboxGlobals(
            _PRE_IMPORTED, __builtins__=safe_builtins, show_object=show_object
        )

        # In analysis mode, provide json module for structured output
        if exec_mode == "analysis":
            safe_globals["json"] = json

        # Execute user code
        exec(code, safe_globals)

        files = {}

        if exec_mode == "analysis":
            # Analysis mode: code sets a 'result' dict variable with analysis data
            analysis_data = safe_globals.get("result")
            if not isinstance(analysis_data, dict):
                raise ValueError(
                    "Analysis mode: code must set a 'result' variable (dict) "
                    "with the analysis output."
                )
            with open("/sandbox/output/analysis.json", "w") as f:
                json.dump(analysis_data, f)
            files["analysis.json"] = "/sandbox/output/analysis.json"
        elif exec_mode == "2d":
            # 2D mode: expect DXF file at /sandbox/output/result.dxf
            dxf_path = "/sandbox/output/result.dxf"
            if os.path.exists(dxf_path):
                import ezdxf
                document = ezdxf.readfile(dxf_path)
                if document.units != ezdxf.units.MM:
                    raise ValueError(
                        "dxf_units_not_millimetres: generated coordinates must be in mm; "
                        "set doc.units = ezdxf.units.MM before saving"
                    )
                files["result.dxf"] = dxf_path
            else:
                raise ValueError(
                    "2D mode: expected DXF output at /sandbox/output/result.dxf but file was not created. "
                    "Ensure code calls doc.saveas('/sandbox/output/result.dxf')."
                )
        else:
            # 3D mode: require show_object(result) or 'result' variable
            if not shown_objects:
                if "result" in safe_globals:
                    shown_objects["result"] = safe_globals["result"]

            if not shown_objects:
                raise ValueError("No result found. Code must call show_object(result) or define a 'result' variable.")

            # Export each result object
            b3d = sys.modules.get("build123d")
            for name, obj in shown_objects.items():
                step_path = f"/sandbox/output/{name}.step"
                stl_path = f"/sandbox/output/{name}.stl"

                if b3d is not None and isinstance(obj, b3d.Shape):
                    # build123d uses the same OCP kernel but has its own shape
                    # wrapper and exporters. Keep this path native so generated
                    # build123d code is not coerced through CadQuery internals.
                    b3d.export_step(obj, step_path)
                    files[f"{name}.step"] = step_path
                    b3d.export_stl(obj, stl_path)
                    files[f"{name}.stl"] = stl_path
                    continue

                import cadquery as cq
                if isinstance(obj, cq.Assembly):
                    # Preserve component bodies and hierarchy in STEP.  STL has
                    # no assembly/component topology; coincident faces between
                    # touching closed components become a non-manifold mesh if
                    # the raw compound is tessellated.  Boolean-fuse the preview
                    # shape so the exported surface is a real watertight model.
                    # Disconnected components remain multiple closed solids.
                    obj.save(step_path)
                    files[f"{name}.step"] = step_path
                    try:
                        from mesh_normalization import export_fused_step_preview

                        export_fused_step_preview(step_path, stl_path)
                        files[f"{name}.stl"] = stl_path
                    except Exception as e:
                        print(f"Warning: STL export for assembly failed: {e}")
                else:
                    # Auto-fuse: if result contains multiple disconnected solids, try to fuse
                    try:
                        solids = obj.val().Solids() if hasattr(obj.val(), 'Solids') else []
                        if len(solids) > 1:
                            print(f"Warning: result contains {len(solids)} disconnected solids, attempting to fuse...")
                            fused = solids[0]
                            for s in solids[1:]:
                                fused = fused.fuse(s)
                            obj = cq.Workplane().add(cq.Shape(fused.wrapped))
                            print(f"Auto-fuse complete: merged into 1 solid")
                    except Exception as e:
                        print(f"Auto-fuse skipped: {e}")

                    # Regular Workplane object
                    cq.exporters.export(obj, step_path, exportType="STEP")
                    files[f"{name}.step"] = step_path

                    cq.exporters.export(obj, stl_path, exportType="STL")
                    files[f"{name}.stl"] = stl_path

        # Explicit SVG exports use the same verified output contract as STEP
        # and DXF. Do not advertise an absent or malformed preview as success.
        svg_path = "/sandbox/output/result.svg"
        if os.path.isfile(svg_path):
            import xml.etree.ElementTree as ET
            if ET.parse(svg_path).getroot().tag not in {"svg", "{http://www.w3.org/2000/svg}svg"}:
                raise ValueError("SVG output must contain an SVG document root")
            files["result.svg"] = svg_path

        # Write success result
        result = {"status": "success", "files": files}
        with open("/sandbox/output/result.json", "w") as f:
            json.dump(result, f)

    except Exception as e:
        tb = traceback.format_exc()
        error_result = {
            "status": "error",
            "error_type": type(e).__name__,
            "error_message": str(e),
            "traceback": tb,
        }
        with open("/sandbox/output/result.json", "w") as f:
            json.dump(error_result, f)
        sys.exit(1)


if __name__ == "__main__":
    main()
