"""Independent DXF readability measurement for the 2D CADTest route."""

import hashlib
import json
import sys
from pathlib import Path

import ezdxf


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def score(dxf: Path) -> dict:
    document = ezdxf.readfile(dxf)
    entities = len(document.modelspace())
    readable = entities > 0 and document.units == ezdxf.units.MM
    return {
        "schema_version": "cad-eval-score.v1",
        "passed": readable,
        "geometry_valid": readable,
        "requirements_passed": 1 if readable else 0,
        "requirements_total": 1,
        "checks": [
            {
                "id": "dxf.readable.v1",
                "status": "pass" if readable else "fail",
                "role": "required",
                "observed": {"entities": entities, "units": document.units},
            }
        ],
        "scorer_error": False,
        "artifact_sha256": digest(dxf),
    }


try:
    result = score(Path(sys.argv[1]))
except Exception as exc:
    result = {
        "passed": False,
        "geometry_valid": False,
        "requirements_passed": 0,
        "requirements_total": 1,
        "checks": [],
        "scorer_error": True,
        "error": f"{type(exc).__name__}: {exc}",
    }
Path(sys.argv[3]).write_text(json.dumps(result, indent=2), encoding="utf-8")
print(json.dumps({"passed": result["passed"], "scorer_error": result["scorer_error"]}))
