"""Real FreeCAD surfaces, selector ambiguity and revision binding (no doubles)."""
import json
import sys
from uuid import uuid4
sys.path.insert(0, "/opt/cad-agent")
import FreeCAD as App
import Part
from freecad_topology import TopologyResolutionError, resolve_topology_selector


def main():
    document = App.newDocument("TopologyAcceptance")
    obj = document.addObject("PartDesign::Feature", "Measured")
    revision = str(uuid4())
    selector = {"schema_version": "topology-selector.v1", "backend": "freecad",
                "revision_id": revision, "object_name": obj.Name,
                "subelement_kind": "face", "geometry": "planar", "axis": "x", "extreme": "max"}
    results = []
    obj.Shape = Part.makeCylinder(5, 10)
    try:
        bad = resolve_topology_selector(document, selector, expected_revision_id=revision)
    except TopologyResolutionError:
        results.append("cylinder_side_rejected")
    else:
        actual = obj.getSubObject(bad["subelement_name"])
        raise AssertionError(f"planar selector accepted {type(actual.Surface).__name__}")
    obj.Shape = Part.makeBox(10, 12, 14)
    result = resolve_topology_selector(document, selector, expected_revision_id=revision)
    assert obj.getSubObject(result["subelement_name"]).Surface.TypeId == "Part::GeomPlane"
    assert result["signature"]["position_mm"] == 10
    results.append("box_plane_measured")
    # Both top faces satisfy the selector; area is not an authorized tie breaker.
    obj.Shape = Part.makeCompound([Part.makeBox(10, 10, 5),
                                   Part.makeBox(4, 7, 5, App.Vector(20, 0, 0))])
    try:
        resolve_topology_selector(document, {**selector, "axis": "z"}, expected_revision_id=revision)
    except TopologyResolutionError:
        results.append("different_area_coplanar_faces_rejected")
    else:
        raise AssertionError("ambiguous coplanar selector chose an arbitrary area")
    try:
        resolve_topology_selector(document, selector, expected_revision_id=str(uuid4()))
    except TopologyResolutionError:
        results.append("stale_revision_rejected")
    else:
        raise AssertionError("stale revision accepted")
    print("CAD_TOPOLOGY_ACCEPTANCE=" + json.dumps({"passed": results}), flush=True)


main()
