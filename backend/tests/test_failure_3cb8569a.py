"""Regression: repaired CAD failure must not hide the final workflow failure."""
import numpy as np
import pytest
import trimesh

from app.services.event_relay import _project_task_error
from sandbox.geometry_validation import _validate_stl
from sandbox.dfm_validation import _metrics


@pytest.mark.parametrize('repair_status',['running','failed'])
def test_final_workflow_error_wins_over_repaired_historical_failure(repair_status):
    error = _project_task_error(workflow_status="failed", workflow_error={
        "error_code": "agent_v2_workflow_failed", "error_message": "Activity task timed out",
    }, steps=[{
        "status": "failed", "step_index": 3, "error_code": "cad_execution_failed",
        "error_message": "Execution failed", "attempts": [],
    }, {"status": repair_status, "step_index": 60001,
        "error_code":"agent_v2_workflow_failed","error_message":"Activity task timed out"}])
    assert error["code"] == "agent_v2_workflow_failed"
    assert error["message"] == "Activity task timed out"
    assert error["category"] == "timeout"


@pytest.mark.parametrize("hole", [False, True])
def test_dfm_and_geometry_agree_on_mesh_normalization(tmp_path, hole):
    box = trimesh.creation.box(extents=[60, 40, 25])
    faces = box.faces[:-1] if hole else box.faces
    mesh = trimesh.Trimesh(vertices=box.vertices, faces=np.vstack([faces, faces[0]]), process=False)
    path = tmp_path / 'seam.stl'
    mesh.export(path)
    geometric = _validate_stl(path, role='model')
    metrics, _ = _metrics(path)
    assert metrics['is_watertight'] == geometric['is_watertight'] == (not hole)


@pytest.mark.asyncio
@pytest.mark.parametrize('part_type,text',[
    ('enclosure','创建一个 60 × 40 × 25 mm 的两腔电子盒，壁厚 2 mm\n\n开孔与装配依据：未提供'),
    ('enclosure','创建一体盒，不要拆分装配'),
    ('assembly','创建盒体和可拆卸盒盖的装配体'),
])
async def test_requirement_context_does_not_override_semantic_part_type(part_type,text):
    import json
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from app.agent.planner import Planner
    response=SimpleNamespace(choices=[SimpleNamespace(finish_reason='stop',message=SimpleNamespace(content=json.dumps({
        'description':text,'part_type':part_type,'dimensions':{'width':60,'depth':40,'height':25},'features':[]})))])
    planner=Planner()
    planner._client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=AsyncMock(return_value=response))))
    plan=await planner.plan_new([{'role':'user','content':text}])
    assert plan.part_type==part_type
