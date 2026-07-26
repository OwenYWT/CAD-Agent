from types import SimpleNamespace

import pytest
import pytest_asyncio

from app.agent.orchestrator import Orchestrator
from app.agent import run_store
from app.agent.state_machine import ExecutionStateMachine
from app.config import settings
from app.storage import history


@pytest_asyncio.fixture
async def temp_db(tmp_path, monkeypatch):
    db_file = tmp_path / 'history_run_store.db'
    monkeypatch.setattr(settings, 'history_db_path', str(db_file))
    await history.close_db()
    yield db_file
    await history.close_db()


@pytest.mark.asyncio
async def test_run_store_records_run_step_and_artifact(temp_db):
    run = await run_store.create_run(
        'session-1',
        'Design a simple bracket',
        panel_id='panel-1',
        capability='auto',
    )

    assert run['session_id'] == 'session-1'
    assert run['panel_id'] == 'panel-1'
    assert run['status'] == 'running'

    step = await run_store.start_step(
        run['id'],
        'plan_design',
        {'prompt': 'Design a simple bracket'},
    )
    assert step['step_index'] == 1
    assert step['status'] == 'running'
    assert step['input']['prompt'] == 'Design a simple bracket'

    step = await run_store.complete_step(
        step['id'],
        {'intent': 'generate', 'design_brief': {'part_type': 'bracket'}},
    )
    assert step['status'] == 'succeeded'
    assert step['output']['intent'] == 'generate'

    artifact = await run_store.record_artifact(
        run['id'],
        step['id'],
        'code',
        '/tmp/bracket.py',
        {'language': 'cadquery'},
    )
    assert artifact['artifact_type'] == 'code'
    assert artifact['path'] == '/tmp/bracket.py'
    assert artifact['metadata']['language'] == 'cadquery'

    await run_store.complete_run(run['id'], status='succeeded', result_request_id='req-1')

    stored_run = await run_store.get_run(run['id'])
    stored_steps = await run_store.list_steps(run['id'])
    stored_artifacts = await run_store.list_artifacts(run['id'])

    assert stored_run['status'] == 'succeeded'
    assert stored_run['result_request_id'] == 'req-1'
    assert stored_steps[0]['step_type'] == 'plan_design'
    assert stored_steps[0]['status'] == 'succeeded'
    assert stored_artifacts[0]['artifact_type'] == 'code'


@pytest.mark.asyncio
async def test_run_store_marks_failed_steps_and_runs(temp_db):
    run = await run_store.create_run('session-2', 'Make a gear housing')
    step = await run_store.start_step(run['id'], 'execute_cad_code', {'code': '...'})

    failed = await run_store.fail_step(
        step['id'],
        {'type': 'ExecutionError', 'message': 'Sandbox failed'},
    )

    stored_run = await run_store.get_run(run['id'])
    assert failed['status'] == 'failed'
    assert failed['error']['message'] == 'Sandbox failed'
    assert stored_run['status'] == 'failed'


@pytest.mark.asyncio
async def test_terminal_step_cannot_be_overwritten(temp_db):
    run = await run_store.create_run('session-lock', 'Protect terminal step')
    step = await run_store.start_step(run['id'], 'execute_cad_code', {'attempt': 1})
    completed = await run_store.complete_step(step['id'], {'success': True})

    failed_late = await run_store.fail_step(step['id'], {'type': 'LateError', 'message': 'late failure'})
    completed_late = await run_store.complete_step(step['id'], {'success': False})
    stored_step = await run_store.get_step(step['id'])

    assert completed['status'] == 'succeeded'
    assert failed_late['status'] == 'succeeded'
    assert completed_late['status'] == 'succeeded'
    assert stored_step['output']['success'] is True
    assert stored_step['error'] is None


@pytest.mark.asyncio
async def test_execute_code_with_run_id_writes_timeline(temp_db, monkeypatch, tmp_path):
    orchestrator = Orchestrator()
    monkeypatch.setattr('app.agent.state_machine.validate_code', lambda code: (True, None))
    monkeypatch.setattr('app.agent.state_machine.analyze_code', lambda code: [])
    monkeypatch.setattr(orchestrator, '_copy_output_files', lambda work_dir, request_id, output_formats: {'dxf': f'/api/files/{request_id}/result.dxf'})
    monkeypatch.setattr(orchestrator, '_extract_params', lambda code: {})
    monkeypatch.setattr(orchestrator, '_find_stl_in_output', lambda work_dir: None)
    fake_work_dir = tmp_path / 'work'
    fake_work_dir.mkdir()
    async def fake_execute(code, mode='3d'):
        return SimpleNamespace(
            success=True,
            work_dir=fake_work_dir,
            execution_time_ms=12,
        )

    orchestrator.executor.execute = fake_execute

    run = await run_store.create_run('session-3', 'Execute code', panel_id='panel-3')
    response = await orchestrator.execute_code('import ezdxf\nresult.dxf = None', run_id=run['id'])

    stored_steps = await run_store.list_steps(run['id'])
    stored_run = await run_store.get_run(run['id'])
    stored_artifacts = await run_store.list_artifacts(run['id'])

    assert response.success is True
    assert [step['step_type'] for step in stored_steps] == ['execute_code']
    assert stored_steps[0]['status'] == 'succeeded'
    assert stored_run['status'] == 'succeeded'
    assert stored_artifacts[0]['artifact_type'] == 'dxf'
    assert stored_artifacts[0]['path'].endswith('result.dxf')


@pytest.mark.asyncio
async def test_generate_repair_attempts_are_persisted_as_steps(temp_db, monkeypatch, tmp_path):
    orchestrator = Orchestrator()
    monkeypatch.setattr('app.agent.state_machine.validate_code', lambda code: (True, None))
    monkeypatch.setattr('app.agent.state_machine.analyze_code', lambda code: [])
    monkeypatch.setattr(orchestrator, '_copy_output_files', lambda work_dir, request_id, output_formats: {'step': f'/api/files/{request_id}/result.step'})
    monkeypatch.setattr(orchestrator, '_extract_params', lambda code: {})
    monkeypatch.setattr(orchestrator, '_find_stl_in_output', lambda work_dir: None)

    first_work_dir = tmp_path / 'failed-work'
    second_work_dir = tmp_path / 'success-work'
    first_work_dir.mkdir()
    second_work_dir.mkdir()
    results = [
        SimpleNamespace(
            success=False,
            work_dir=first_work_dir,
            execution_time_ms=10,
            error_type='SyntaxError',
            error_message='first code failure',
            traceback='SyntaxError: invalid syntax',
        ),
        SimpleNamespace(success=True, work_dir=second_work_dir, execution_time_ms=12),
    ]

    async def fake_execute(code, mode='3d'):
        return results.pop(0)

    async def fake_fix_error(code, error, plan):
        return 'print("fixed")'

    orchestrator.executor.execute = fake_execute
    orchestrator.code_gen.fix_error = fake_fix_error

    run = await run_store.create_run('session-repair', 'Repair then succeed', panel_id='panel-repair')
    response = await orchestrator._execute_with_retry(
        'req-repair',
        'print("broken")',
        None,
        ['step'],
        None,
        'Repair then succeed',
        is_2d=False,
        run_id=run['id'],
    )
    await orchestrator._complete_run(run['id'], response)

    stored_steps = await run_store.list_steps(run['id'])
    stored_run = await run_store.get_run(run['id'])

    assert response.success is True
    assert stored_run['status'] == 'succeeded'
    assert [step['step_type'] for step in stored_steps] == [
        'execute_cad_code',
        'repair_code',
        'execute_cad_code',
        'finalize_result',
    ]
    assert stored_steps[0]['status'] == 'failed'
    assert stored_steps[0]['error']['message'] == 'first code failure'
    assert stored_steps[0]['input']['code'] == 'print("broken")'
    assert stored_steps[0]['input']['output_formats'] == ['step']
    assert stored_steps[0]['input']['user_prompt'] == 'Repair then succeed'
    assert stored_steps[1]['status'] == 'succeeded'
    assert stored_steps[1]['output']['stage'] == 'execution'
    assert stored_steps[1]['output']['repaired_code'] == 'print("fixed")'
    assert stored_steps[-1]['output']['request_id'] == 'req-repair'


@pytest.mark.asyncio
async def test_state_machine_records_step_runner_decisions(temp_db, monkeypatch, tmp_path):
    orchestrator = Orchestrator()
    monkeypatch.setattr('app.agent.state_machine.validate_code', lambda code: (True, None))
    monkeypatch.setattr('app.agent.state_machine.analyze_code', lambda code: [])
    monkeypatch.setattr(orchestrator, '_copy_output_files', lambda work_dir, request_id, output_formats: {'step': f'/api/files/{request_id}/result.step'})
    monkeypatch.setattr(orchestrator, '_extract_params', lambda code: {})
    monkeypatch.setattr(orchestrator, '_find_stl_in_output', lambda work_dir: None)

    first_work_dir = tmp_path / 'runner-failed-work'
    second_work_dir = tmp_path / 'runner-success-work'
    first_work_dir.mkdir()
    second_work_dir.mkdir()
    results = [
        SimpleNamespace(
            success=False,
            work_dir=first_work_dir,
            execution_time_ms=10,
            error_type='SyntaxError',
            error_message='first code failure',
            traceback='SyntaxError: invalid syntax',
        ),
        SimpleNamespace(success=True, work_dir=second_work_dir, execution_time_ms=12),
    ]

    async def fake_execute(code, mode='3d'):
        return results.pop(0)

    async def fake_fix_error(code, error, plan):
        return 'print("fixed")'

    orchestrator.executor.execute = fake_execute
    orchestrator.code_gen.fix_error = fake_fix_error
    run = await run_store.create_run('session-runner', 'Runner repair then succeed', panel_id='panel-runner')
    state_machine = ExecutionStateMachine(
        orchestrator=orchestrator,
        request_id='req-runner',
        code='print("broken")',
        plan=None,
        output_formats=['step'],
        user_prompt='Runner repair then succeed',
        run_id=run['id'],
    )

    response = await state_machine.run()

    next_steps = [item.detail.get('next_step') for item in state_machine.trace if item.detail.get('next_step')]
    assert response.success is True
    assert next_steps[:3] == ['repair_code', 'execute_cad_code', 'finalize_result']


@pytest.mark.asyncio
async def test_resume_run_uses_resume_available_input(temp_db, monkeypatch, tmp_path):
    orchestrator = Orchestrator()
    monkeypatch.setattr('app.agent.state_machine.validate_code', lambda code: (True, None))
    monkeypatch.setattr('app.agent.state_machine.analyze_code', lambda code: [])
    monkeypatch.setattr(orchestrator, '_copy_output_files', lambda work_dir, request_id, output_formats: {'step': f'/api/files/{request_id}/result.step'})
    monkeypatch.setattr(orchestrator, '_extract_params', lambda code: {})
    monkeypatch.setattr(orchestrator, '_find_stl_in_output', lambda work_dir: None)

    work_dir = tmp_path / 'resume-work'
    work_dir.mkdir()
    executed = []

    async def fake_execute(code, mode='3d'):
        executed.append((code, mode))
        return SimpleNamespace(success=True, work_dir=work_dir, execution_time_ms=12)

    orchestrator.executor.execute = fake_execute
    run = await run_store.create_run('session-resume', 'Resume repaired code', panel_id='panel-resume')
    resume_step = await run_store.start_step(run['id'], 'resume_available', {'next_step': 'execute_cad_code'})
    await run_store.complete_step(
        resume_step['id'],
        {
            'resumable': True,
            'next_step': 'execute_cad_code',
            'resume_input': {
                'code': 'print("fixed")',
                'output_formats': ['step'],
                'user_prompt': 'Resume repaired code',
                'is_2d': False,
            },
        },
        status='blocked',
    )
    await run_store.complete_run(run['id'], status='blocked')

    response = await orchestrator.resume_run(run['id'])
    stored_run = await run_store.get_run(run['id'])
    stored_steps = await run_store.list_steps(run['id'])

    assert response.success is True
    assert executed == [('print("fixed")', '3d')]
    assert stored_run['status'] == 'succeeded'
    assert stored_steps[0]['step_type'] == 'resume_available'
    assert stored_steps[0]['status'] == 'succeeded'
    assert stored_steps[0]['output']['resumed'] is True
    assert stored_steps[-1]['step_type'] == 'finalize_result'

    with pytest.raises(ValueError, match='\u6ca1\u6709\u53ef\u7ee7\u7eed'):
        await orchestrator.resume_run(run['id'])
