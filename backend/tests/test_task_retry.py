from copy import deepcopy
from uuid import uuid4
import pytest
from app.services.task_retry import retry_input


@pytest.mark.asyncio
@pytest.mark.parametrize('objective', ['继续', ' 重试！ ', 'continue.',
    '当前上下文：机械设计。请先限定修改范围，再执行以下请求并验证结果：继续'])
async def test_continuation_cannot_start_a_workflow_without_a_concrete_objective(objective):
    from app.services.durable_submission import submit_durable_workflow
    task=original();task['request_payload']['objective']=objective
    # Exercise the actual shared submission entry used by retry and new requests.
    # It must reject before any database, provider or workflow side effect.
    with pytest.raises(ValueError, match='缺少建模目标'):
        await submit_durable_workflow(None, idempotency_key='retry-test', **retry_input(task))


def original():
    return {'id':str(uuid4()),'project_id':str(uuid4()),'kind':'mcad.agent.v2.generate','status':'failed',
        'request_payload':{'branch_id':str(uuid4()),'expected_base_revision_id':str(uuid4()),
          'expected_state_version':3,'operation':'generate','objective':'手机壳，保留用户原始尺寸 60 × 40 × 8 mm',
          'modeling_backend':'auto','output_formats':['step','stl'],
          'manufacturing_profile':{'material_name':'TPU'}}}


def test_retry_preserves_original_objective_profile_outputs_and_generation():
    task=original();before=deepcopy(task);args=retry_input(task)
    assert args['objective']==task['request_payload']['objective']
    assert args['manufacturing_profile']==task['request_payload']['manufacturing_profile']
    assert args['expected_state_version']==3
    assert str(args['expected_base_revision_id'])==task['request_payload']['expected_base_revision_id']
    assert args['output_formats']==['step','stl']
    assert task==before


@pytest.mark.parametrize('status',['running','planning','pending','waiting_confirmation','succeeded'])
def test_active_and_successful_tasks_cannot_be_retried(status):
    task=original();task['status']=status
    with pytest.raises(ValueError):retry_input(task)


@pytest.mark.parametrize('field',['structured_modification','revision_restore'])
def test_retry_does_not_reuse_expired_edit_leases_or_restore_approval(field):
    task=original();task['request_payload'][field]={'expired':True}
    with pytest.raises(ValueError):retry_input(task)


def test_retry_preserves_native_selection_and_engineering_basis():
    task = original()
    payload = task['request_payload']
    payload.update(operation='modify', modeling_backend='freecad')
    task['kind'] = 'mcad.agent.v2.modify'
    payload['operation_context'] = {
        'rule': 'explicit_ui_intent', 'source_channel': 'session_websocket',
        'panel_id': 'panel', 'requested_operation': 'modify', 'resolved_operation': 'modify',
        'submission_modeling_backend': 'freecad',
        'base_revision_id': payload['expected_base_revision_id'], 'base_source_kind': 'fcstd_artifact',
        'base_source_id': str(uuid4()), 'base_source_sha256': 'a' * 64,
        'selection_context': {'revision_id': payload['expected_base_revision_id'], 'state_version': 3,
            'feature_ids': [str(uuid4())]},
        'requirement_basis': {'target': '手机壳', 'source_kind': 'reference',
            'source_reference': '用户图纸第 2 页', 'dimensions': '壁厚 2 mm', 'concept_acknowledged': False},
    }
    args = retry_input(task)
    assert args['selection_context'].model_dump(mode='json')['feature_ids'] == payload['operation_context']['selection_context']['feature_ids']
    assert args['operation_context'].requirement_basis.dimensions == '壁厚 2 mm'
    assert str(args['selection_context'].revision_id) == payload['expected_base_revision_id']
