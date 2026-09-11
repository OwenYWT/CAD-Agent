"""Real shape-check diagnostic must retain its kernel failure classification."""
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, "/opt/cad-agent")
import freecad_entry as runner


def main():
    task = json.loads(Path(os.environ["CAD_INVALID_NATIVE_TASK"]).read_text())
    try:
        runner.run_task(task)
    except runner.FreeCADRunnerError as error:
        assert error.code == "shape_check_failed", (error.code, str(error))
        assert error.action == "feature.fillet" and error.details["object"]
        print("CAD_NATIVE_FAILURE_CLASSIFICATION=" + json.dumps({
            "code": error.code, "action": error.action, "details": error.details,
            "message": str(error), "no_exported_model": not list(runner.OUTPUT_ROOT.glob("*.FCStd"))}), flush=True)
    else:
        raise AssertionError("the impossible fillet unexpectedly passed native validation")


try:
    main()
except BaseException:
    import traceback
    traceback.print_exc()
    os._exit(1)
