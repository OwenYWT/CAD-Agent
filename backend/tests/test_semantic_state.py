from copy import deepcopy
from uuid import uuid4

from app.freecad.semantic_state import bounded_agent_context, project_semantic_state, semantic_delta


def test_feature_identity_dependencies_and_local_parameter_delta():
    doc = uuid4()
    state = {"objects": [{"name": "Sketch", "type_id": "Sketcher::SketchObject", "out": []},
                         {"name": "Pad", "type_id": "PartDesign::Pad", "out": ["Sketch"]}],
             "root_objects": ["Pad"], "parameters": [{"object_name": "Pad", "id": "Pad.Length", "value": 6}]}
    before = project_semantic_state(doc, state)
    changed = deepcopy(state)
    changed["parameters"][0]["value"] = 8
    after = project_semantic_state(doc, changed)
    assert [f["id"] for f in before["features"]] == [f["id"] for f in after["features"]]
    assert after["features"][1]["dependencies"] == [before["features"][0]["id"]]
    delta = semantic_delta(before, after)
    assert len(delta["upserted"]) == 1 and delta["upserted"][0]["kernel_name"] == "Pad"
    assert delta["removed"] == []
    assert after["features"][1]["intent"] is None
    assert project_semantic_state(uuid4(), state)["features"][0]["id"] != before["features"][0]["id"]


def test_agent_context_bounded_without_silently_claiming_completeness():
    state = {"document": "Large", "objects": [{"name": f"Feature{i}", "out": [],
             "properties": {"huge": "x" * 10000}} for i in range(300)], "parameters": []}
    result = bounded_agent_context(state, target_names=["Feature299"])
    assert result["summary"]["object_count"] == 300
    assert result["summary"]["omitted_inventory_count"] == 220
    assert result["features"][0]["name"] == "Feature299"
    assert len(result["features"]) == 1
    assert "huge" not in str(result)


def test_native_hierarchy_never_invents_containment_from_dependencies_or_rewrites_history():
    doc, revision = uuid4(), uuid4()
    state = {"objects": [{"name": "Body", "type_id": "PartDesign::Body", "out": ["Pad", "Sketch"]},
                         {"name": "Pad", "type_id": "PartDesign::Pad", "out": ["Sketch"]},
                         {"name": "Sketch", "type_id": "Sketcher::SketchObject", "out": []}]}
    before = project_semantic_state(doc, state, revision_id=revision, previous={"features": []})
    assert before["hierarchy_status"] == "unavailable"
    assert all(f["structure"] is None for f in before["features"])
    measured = deepcopy(state)
    for obj in measured["objects"]:
        obj["structure"] = {"status": "measured", "category": "feature", "members": [], "body_tip": None}
    measured["objects"][0]["structure"].update(category="body", members=["Sketch", "Pad"], body_tip="Pad")
    after = project_semantic_state(doc, measured, revision_id=uuid4(), previous=before)
    assert after["hierarchy_status"] == "measured"
    body, pad, sketch = after["features"]
    assert body["structure"]["member_ids"] == [sketch["id"], pad["id"]]
    assert body["structure"]["body_tip_id"] == pad["id"]
    assert pad["structure"]["container_ids"] == [body["id"]]
    assert pad["dependencies"] == [sketch["id"]]
    assert [f["last_modified"] for f in before["features"]] == [f["last_modified"] for f in after["features"]]
