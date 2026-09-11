"""Resolve every published binding against its actual downloaded FCStd."""
import hashlib
import json
import os
from pathlib import Path
import sys
import traceback
from uuid import uuid4

sys.path.insert(0, "/opt/cad-agent")
import FreeCAD as App
from freecad_topology import TopologyResolutionError, resolve_topology_selector


def main():
    inputs = json.loads(Path(os.environ["CAD_SEMANTIC_BINDINGS"]).read_text())
    model = Path(inputs["fcstd"])
    snapshot = inputs["snapshot"]
    assert hashlib.sha256(model.read_bytes()).hexdigest() == snapshot["fcstd"]["sha256"]
    document = App.openDocument(str(model))
    count = {"planar": 0, "circular": 0}
    for feature in snapshot["features"]:
        for selector in feature["topology_bindings"]:
            assert selector["revision_id"] == snapshot["head_revision_id"]
            result = resolve_topology_selector(document, selector, expected_revision_id=snapshot["head_revision_id"])
            obj = document.getObject(result["object_name"])
            actual = obj.getSubObject(result["subelement_name"])
            assert actual is not None, {"selector": selector, "resolved": result,
                "resolved_name": repr(obj.resolveSubElement(result["subelement_name"], False, 0))}
            if selector["geometry"] == "planar":
                assert actual.Surface.TypeId == "Part::GeomPlane"
            else:
                assert actual.Curve.TypeId == "Part::GeomCircle"
                assert abs(actual.Curve.Radius - selector["radius_mm"]) <= selector["tolerance_mm"]
            count[selector["geometry"]] += 1
            try:
                resolve_topology_selector(document, selector, expected_revision_id=str(uuid4()))
            except TopologyResolutionError:
                pass
            else:
                raise AssertionError("published selector accepted the wrong revision")
    assert all(count.values()), count
    App.closeDocument(document.Name)
    print("CAD_SEMANTIC_BINDINGS=" + json.dumps({"revision_id": snapshot["head_revision_id"],
        "resolved_actual_kernel_types": count, "all_stale_revisions_rejected": True,
        "verified_fcstd_sha256": snapshot["fcstd"]["sha256"]}), flush=True)


try:
    main()
except BaseException:
    traceback.print_exc()
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(1)
