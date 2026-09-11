from copy import deepcopy
from uuid import uuid4
import pytest

from app.freecad.semantic_state import bounded_agent_context, project_semantic_state


def native_state():
    return {"root_objects": ["Pad"], "objects": [{"name": "Pad", "type_id": "PartDesign::Pad",
        "geometry_sha256": "first", "shape": {"bounds_mm": {"min": [0, 0, 0], "max": [80, 60, 8]}},
        "inspection": {"topology": {"total_faces": 2, "total_edges": 0, "items": [
            {"kind": "face", "geometry_type": "Part::GeomPlane", "center": [40, 30, 8], "normal": [0, 0, 1], "area_mm2": 4800},
            {"kind": "face", "geometry_type": "Part::GeomCylinder", "center": [43, 30, 4], "normal": [1, 0, 0], "area_mm2": 150}
        ]}}}], "parameters": [{"id": "Pad.Length", "object_name": "Pad", "value": 8}]}


def test_feature_history_changes_only_with_verified_feature_content():
    doc, first, second, third = uuid4(), uuid4(), uuid4(), uuid4()
    state = native_state()
    a = project_semantic_state(doc, state, revision_id=first, previous={"features": []})
    b = project_semantic_state(doc, state, revision_id=second, previous=a)
    assert b["features"][0]["revision_created"] == str(first)
    assert b["features"][0]["last_modified"] == str(first)
    modified = deepcopy(state)
    modified["objects"][0]["geometry_sha256"] = "changed"
    c = project_semantic_state(doc, modified, revision_id=third, previous=b)
    assert c["features"][0]["revision_created"] == str(first)
    assert c["features"][0]["last_modified"] == str(third)
    expanded_measurement = deepcopy(state)
    expanded_measurement["objects"][0]["shape"]["new_measurement"] = 123
    measured = project_semantic_state(doc, expanded_measurement, revision_id=third, previous=b)
    assert measured["features"][0]["last_modified"] == str(first)


def test_topology_bindings_are_revision_scoped_and_never_guess_plane_from_normal():
    revision = uuid4()
    result = project_semantic_state(uuid4(), native_state(), revision_id=revision)
    bindings = result["features"][0]["topology_bindings"]
    assert bindings
    assert all(b["revision_id"] == str(revision) and b["axis"] == "z" for b in bindings)
    assert "Face1" not in str(bindings)
    ambiguous = native_state()
    topology = ambiguous["objects"][0]["inspection"]["topology"]
    topology["items"].append({**topology["items"][0], "area_mm2": 10})
    topology["total_faces"] = 3
    result = project_semantic_state(uuid4(), ambiguous, revision_id=revision)
    assert result["features"][0]["topology_bindings"] == []
    topology["items"][-1] = {"kind": "face", "status": "unavailable"}
    assert project_semantic_state(uuid4(), ambiguous, revision_id=revision)["features"][0]["topology_bindings"] == []


def test_l0_reports_measured_dimensions_features_and_exact_revision_dfm_or_unknown():
    state = native_state()
    summary = bounded_agent_context(state)["summary"]
    assert summary["main_dimensions_mm"] == {"x": 80, "y": 60, "z": 8}
    assert summary["key_features"][0]["type_id"] == "PartDesign::Pad"
    assert summary["dfm"]["status"] == "unavailable"
    state["dfm_summary"] = {"status": "failed", "revision_id": str(uuid4()), "issues": ["thin_wall"]}
    assert bounded_agent_context(state)["summary"]["dfm"]["status"] == "failed"


@pytest.mark.parametrize("type_id", ["App::Plane", "App::Line", "App::Point", "App::Origin",
    "PartDesign::Plane", "PartDesign::Line", "PartDesign::Point", "PartDesign::CoordinateSystem"])
def test_reference_geometry_does_not_publish_physical_subelement_bindings(type_id):
    state = native_state()
    state["objects"][0]["type_id"] = type_id
    result = project_semantic_state(uuid4(), state, revision_id=uuid4())
    assert result["features"][0]["topology_bindings"] == []
