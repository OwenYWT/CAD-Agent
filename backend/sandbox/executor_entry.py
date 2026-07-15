"""
Sandbox executor entry point.
Runs inside Docker container with restricted globals.
Reads <input>/input.py, executes it, exports results to <output>/.

Paths default to the container mounts (/sandbox/input, /sandbox/output) and are
overridable via CAD_SANDBOX_INPUT / CAD_SANDBOX_OUTPUT so the same script backs
the SANDBOX_RUNTIME=local host-subprocess runtime.
"""
import builtins
import json
import os
import sys
import traceback

# Capture the real import before any restriction
_real_import = builtins.__import__

INPUT_DIR = os.environ.get("CAD_SANDBOX_INPUT", "/sandbox/input")
OUTPUT_DIR = os.environ.get("CAD_SANDBOX_OUTPUT", "/sandbox/output")

# Ensure output directory exists
os.makedirs(OUTPUT_DIR, exist_ok=True)


def main():
    try:
        # Read input code. Always UTF-8: the host writes it as UTF-8 and CAD prompts
        # are frequently Chinese — the default locale (e.g. cp936 on zh-Hans Windows,
        # which the SANDBOX_RUNTIME=local path hits) would raise UnicodeDecodeError.
        with open(os.path.join(INPUT_DIR, "input.py"), "r", encoding="utf-8") as f:
            code = f.read()

        # Generated code is taught to write to the container paths (e.g.
        # doc.saveas('/sandbox/output/result.dxf')). When running outside the
        # container those paths don't exist — remap the literals to the real dirs.
        if OUTPUT_DIR != "/sandbox/output":
            code = code.replace("/sandbox/output", OUTPUT_DIR)
        if INPUT_DIR != "/sandbox/input":
            code = code.replace("/sandbox/input", INPUT_DIR)

        # Read execution mode: "2d", "3d" (default), or "analysis"
        exec_mode = "3d"
        mode_path = os.path.join(INPUT_DIR, "mode.txt")
        if os.path.exists(mode_path):
            with open(mode_path, "r", encoding="utf-8") as mf:
                exec_mode = mf.read().strip().lower()

        # Pre-import allowed modules
        import cadquery as cq
        import math
        import numpy as np

        _ALLOWED_ROOTS = {
            "cadquery", "cq", "math", "numpy", "np",
            # OCP (OpenCascade) modules — needed for STEP geometry analysis
            "OCP",
        }

        # Pre-imported module shortcuts (returned directly for simple imports)
        _PRE_IMPORTED = {
            "cadquery": cq,
            "cq": cq,
            "math": math,
            "numpy": np,
            "np": np,
        }

        # Try to import ezdxf if available (for 2D DXF generation)
        try:
            import ezdxf as _ezdxf
            _ALLOWED_ROOTS.add("ezdxf")
            _PRE_IMPORTED["ezdxf"] = _ezdxf
        except ImportError:
            pass

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
            return _real_import(name, globals, locals, fromlist, level)

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

        # Build safe globals
        safe_globals = {
            "__builtins__": safe_builtins,
            "cq": cq,
            "cadquery": cq,
            "math": math,
            "numpy": np,
            "np": np,
            "show_object": show_object,
        }

        # Also make ezdxf available if it was imported
        if "ezdxf" in _PRE_IMPORTED:
            safe_globals["ezdxf"] = _PRE_IMPORTED["ezdxf"]

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
            analysis_path = os.path.join(OUTPUT_DIR, "analysis.json")
            with open(analysis_path, "w") as f:
                json.dump(analysis_data, f)
            files["analysis.json"] = analysis_path
        elif exec_mode == "2d":
            # 2D mode: expect DXF file at <output>/result.dxf
            dxf_path = os.path.join(OUTPUT_DIR, "result.dxf")
            if os.path.exists(dxf_path):
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
            for name, obj in shown_objects.items():
                step_path = f"{OUTPUT_DIR}/{name}.step"
                stl_path = f"{OUTPUT_DIR}/{name}.stl"

                if isinstance(obj, cq.Assembly):
                    # Assembly: use .save() for STEP, merge to compound for STL
                    obj.save(step_path)
                    files[f"{name}.step"] = step_path
                    try:
                        compound = obj.toCompound()
                        cq.exporters.export(cq.Workplane().add(compound), stl_path, exportType="STL")
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

        # Write success result
        result = {"status": "success", "files": files}
        with open(os.path.join(OUTPUT_DIR, "result.json"), "w") as f:
            json.dump(result, f)

    except Exception as e:
        tb = traceback.format_exc()
        error_result = {
            "status": "error",
            "error_type": type(e).__name__,
            "error_message": str(e),
            "traceback": tb,
        }
        with open(os.path.join(OUTPUT_DIR, "result.json"), "w") as f:
            json.dump(error_result, f)
        sys.exit(1)


if __name__ == "__main__":
    main()
