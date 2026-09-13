"""Selection contract/decision tests; real kernel transport is tested in E2E."""
from copy import deepcopy
from uuid import UUID, uuid4

import pytest

from app.api.documents import OperationRequest
from app.freecad.contracts import FreeCADOperationPlan
from app.freecad.selection import SelectionContextV1, SelectionError, freeze_selection, needs_selection, validate_selected_operations
from app.freecad.semantic_state import project_semantic_state, bounded_agent_context
from app.workflows.temporal import OperationContextV1


def example():
    document, revision = uuid4(), uuid4()
    state = {"objects": [{"name": "Sketch", "out": [], "type_id": "Sketcher::SketchObject", "sketch": {"geometry_count": 2}},
        {"name": "Hole", "type_id": "PartDesign::Hole", "out": ["Sketch"]},
        {"name": "Pad", "type_id": "PartDesign::Pad", "out": []}], "parameters": []}
    projection = project_semantic_state(document, state, revision_id=revision)
    projection.update(revision_id=str(revision), fcstd={"artifact_id": str(uuid4())}, parameter_state_sha256="a"*64)
    selection = SelectionContextV1(revision_id=revision, state_version=2,
        feature_ids=[UUID(projection["features"][1]["id"])])
    return state, projection, selection


def test_versioned_selection_rejects_foreign_stale_and_single_hole_guess():
    _, projection, selection = example()
    args = {"revision_id": selection.revision_id, "state_version": 2, "objective": "修改整个孔特征的直径"}
    frozen = freeze_selection(projection, selection, **args)
    assert frozen["features"][0]["kernel_name"] == "Hole"
    for changed in ({"state_version": 4}, {"revision_id": uuid4()}, {"feature_ids": (uuid4(),)}):
        with pytest.raises(SelectionError):
            freeze_selection(projection, selection.model_copy(update=changed), **args)
    with pytest.raises(SelectionError, match="单孔"):
        freeze_selection(projection, selection, **{**args, "objective": "把这个孔改成 8mm"})
    with pytest.raises(SelectionError, match="边或面"):
        freeze_selection(projection, selection, **{**args, "objective": "把这条边倒圆角"})
    assert needs_selection("将这个孔扩大") and not needs_selection("将 Hole 特征的孔径改成 8mm")


def test_frozen_context_is_in_bounded_provider_input_and_blocks_wrong_feature():
    state, projection, selection = example()
    state["selection_context"] = freeze_selection(projection, selection, revision_id=selection.revision_id,
        state_version=2, objective="修改整个孔特征")
    context = bounded_agent_context(state)
    assert [f["name"] for f in context["features"]] == ["Hole", "Sketch"]
    assert context["selection_context"]["state_version"] == 2
    def plan(name):
        return FreeCADOperationPlan.model_validate({"schema_version": "freecad-operation-plan.v1",
            "document_name": "Model", "operations": [{"op_id": "edit", "action": "property.set",
            "args": {"object": name, "property": "Length", "value": 8}}, {"op_id": "export",
            "action": "document.export", "args": {"formats": ["fcstd", "step", "stl"], "basename": "model"}}]})
    validate_selected_operations(plan("Hole"), state)
    with pytest.raises(SelectionError, match="范围之外"):
        validate_selected_operations(plan("Pad"), state)


def test_selector_requires_exact_measured_binding_not_symmetric_or_incomplete_hint():
    _, projection, selection = example()
    selector = {"revision_id": str(selection.revision_id), "object_name": "Hole", "subelement_kind": "edge",
        "geometry": "circular", "axis": "z", "extreme": "max", "radius_mm": 3,
        "center": {"x": 10, "y": 10, "z": 6}}
    selected = SelectionContextV1.model_validate({**selection.model_dump(), "topology_selector": selector})
    args = {"revision_id": selection.revision_id, "state_version": 2, "objective": "把这条边倒圆角"}
    with pytest.raises(SelectionError, match="唯一"):
        freeze_selection(projection, selected, **args)
    measured = deepcopy(projection)
    measured["features"][1]["topology_bindings"] = [selector]
    assert freeze_selection(measured, selected, **args)["topology_selector"]["radius_mm"] == 3


def test_absent_selection_preserves_old_rest_and_operation_context_serialization():
    revision = uuid4()
    body = {"action": "modify", "expected_base_revision_id": str(revision), "expected_state_version": 0,
        "idempotency_key": "old-client", "lease_token": None, "allow_rebase": False,
        "objective": "change diameter", "modification": None}
    assert OperationRequest.model_validate(body).model_dump(mode="json") == body
    old = {"schema_version": "mcad-operation-context.v1", "rule": "explicit_rest_operation", "source_channel": "rest",
        "panel_id": None, "requested_operation": "modify", "resolved_operation": "modify",
        "requested_modeling_backend": "auto", "submission_modeling_backend": "freecad", "base_revision_id": str(revision),
        "base_source_kind": "fcstd_artifact", "base_source_id": str(uuid4()), "base_source_sha256": "a"*64}
    assert OperationContextV1.model_validate(old).model_dump(mode="json") == old


def test_selected_hole_dependencies_are_readable_but_do_not_authorize_upstream_writes():
    state, projection, selection = example()
    # Real FreeCAD Hole.OutList includes its support Pad; the profile sketch
    # also references that Pad for attachment. Neither reference grants edits.
    state['objects'][1]['out'] = ['Sketch','Pad']
    state['objects'][0]['out'] = ['Pad']
    state['selection_context'] = freeze_selection(projection, selection,
        revision_id=selection.revision_id,state_version=2,objective='修改整个孔特征')
    def plan(action,args):
        return FreeCADOperationPlan.model_validate({'document_name':'Model','operations':[
            {'op_id':'edit','action':action,'args':args},
            {'op_id':'export','action':'document.export','args':{'formats':['fcstd','step'],'basename':'model'}}]})
    validate_selected_operations(plan('sketch.set_constraint',{'sketch':'Sketch','constraint_index':0,
        'expected_type':'Radius','value_mm':4}),state)
    for action,args in [('property.set',{'object':'Pad','property':'Length','value':20}),
                        ('property.set',{'object':'Unknown','property':'Length','value':20}),
                        ('sketch.create',{'name':'SurpriseSketch','body':'OtherBody'}),
                        ('assembly.instance',{'object':'SurpriseLink','source':'Pad','translation_mm':[0,0,0]})]:
        with pytest.raises(SelectionError):
            validate_selected_operations(plan(action,args),state)


def test_unique_edge_does_not_authorize_resizing_an_entire_multi_hole_feature():
    state, projection, selection = example()
    selector = {'revision_id':str(selection.revision_id),'object_name':'Hole','subelement_kind':'edge',
        'geometry':'circular','axis':'z','extreme':'max','radius_mm':3,'center':{'x':10,'y':10,'z':6}}
    projection['features'][1]['topology_bindings']=[selector]
    selected=SelectionContextV1.model_validate({**selection.model_dump(),'topology_selector':selector})
    with pytest.raises(SelectionError,match='子元素'):
        freeze_selection(projection,selected,revision_id=selection.revision_id,state_version=2,objective='把这个孔的直径改成 8 mm')
    state['selection_context']=freeze_selection(projection,selected,revision_id=selection.revision_id,
        state_version=2,objective='将这个孔的边缘倒圆角')
    plan=FreeCADOperationPlan.model_validate({'document_name':'Model','operations':[
        {'op_id':'wrong-whole-feature','action':'property.set','args':{'object':'Hole','property':'Diameter','value':8}},
        {'op_id':'export','action':'document.export','args':{'formats':['fcstd','step'],'basename':'model'}}]})
    with pytest.raises(SelectionError,match='子元素'):
        validate_selected_operations(plan,state)
