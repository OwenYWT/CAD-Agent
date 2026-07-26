from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio

from app.agent import run_store
from app.agent.recovery import classify_resume_state, ensure_run_resume_available, recover_running_runs, recover_stale_runs, sanitize_incomplete_steps
from app.config import settings
from app.storage import history


@pytest_asyncio.fixture
async def temp_db(tmp_path, monkeypatch):
    db_file = tmp_path / 'history_recovery.db'
    monkeypatch.setattr(settings, 'history_db_path', str(db_file))
    await history.close_db()
    yield db_file
    await history.close_db()


@pytest.mark.asyncio
async def test_recover_stale_running_step_marks_run_failed(temp_db):
    run = await run_store.create_run('session-recover', 'make a box', panel_id='panel-recover')
    step = await run_store.start_step(run['id'], 'execute_code', {'mode': '3d'})

    stale_time = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    db = await history.get_db()
    await db.execute('UPDATE agent_runs SET updated_at = ? WHERE id = ?', (stale_time, run['id']))
    await db.execute('UPDATE agent_steps SET started_at = ? WHERE id = ?', (stale_time, step['id']))
    await db.commit()

    recovered = await recover_stale_runs(stale_after_seconds=60)

    stored_run = await run_store.get_run(run['id'])
    stored_step = await run_store.get_step(step['id'])

    assert recovered == 1
    assert stored_run['status'] == 'failed'
    assert stored_run['completed_at'] is not None
    assert stored_step['status'] == 'failed'
    assert stored_step['error']['type'] == 'InterruptedRun'
    assert "\u957f\u65f6\u95f4" in stored_step['error']['message']


@pytest.mark.asyncio
async def test_recover_stale_run_without_step_marks_run_failed(temp_db):
    run = await run_store.create_run('session-recover-empty', 'make a cup', panel_id='panel-recover')
    stale_time = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    db = await history.get_db()
    await db.execute('UPDATE agent_runs SET updated_at = ? WHERE id = ?', (stale_time, run['id']))
    await db.commit()

    recovered = await recover_stale_runs(stale_after_seconds=60)

    stored_run = await run_store.get_run(run['id'])
    assert recovered == 1
    assert stored_run['status'] == 'failed'
    assert stored_run['completed_at'] is not None


@pytest.mark.asyncio
async def test_recover_stale_run_after_repair_marks_blocked_for_manual_resume(temp_db):
    run = await run_store.create_run('session-resumable', 'make a box', panel_id='panel-resumable')
    repair_step = await run_store.start_step(run['id'], 'repair_code', {'attempt': 1})
    await run_store.complete_step(repair_step['id'], {'code_changed': True, 'stage': 'execution', 'repaired_code': 'print("fixed")'})

    stale_time = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    db = await history.get_db()
    await db.execute('UPDATE agent_runs SET status = ?, updated_at = ?, completed_at = NULL WHERE id = ?', ('running', stale_time, run['id']))
    await db.commit()

    recovered = await recover_stale_runs(stale_after_seconds=60)

    stored_run = await run_store.get_run(run['id'])
    stored_steps = await run_store.list_steps(run['id'])

    assert recovered == 1
    assert stored_run['status'] == 'blocked'
    assert stored_run['completed_at'] is not None
    assert stored_steps[-1]['step_type'] == 'resume_available'
    assert stored_steps[-1]['status'] == 'blocked'
    assert stored_steps[-1]['output']['next_step'] == 'execute_cad_code'
    assert stored_steps[-1]['output']['resume_input']['code'] == 'print("fixed")'


@pytest.mark.asyncio
async def test_recover_recent_running_execute_step_marks_resume_available(temp_db):
    run = await run_store.create_run('session-recover-now', 'resume immediately', panel_id='panel-recover-now')
    await run_store.start_step(
        run['id'],
        'execute_cad_code',
        {
            'attempt': 1,
            'mode': '3d',
            'code': 'print("interrupted")',
            'output_formats': ['step'],
            'user_prompt': 'resume immediately',
            'is_2d': False,
        },
    )

    recovered = await recover_running_runs()

    stored_run = await run_store.get_run(run['id'])
    stored_steps = await run_store.list_steps(run['id'])

    assert recovered == 1
    assert stored_run['status'] == 'blocked'
    assert stored_steps[0]['status'] == 'failed'
    assert stored_steps[0]['error']['type'] == 'InterruptedRun'
    assert stored_steps[-1]['step_type'] == 'resume_available'
    assert stored_steps[-1]['status'] == 'blocked'
    assert stored_steps[-1]['output']['next_step'] == 'execute_cad_code'
    assert stored_steps[-1]['output']['resume_input']['code'] == 'print("interrupted")'


@pytest.mark.asyncio
async def test_recover_running_execute_code_alias_marks_resume_available(temp_db):
    long_code = 'print("interrupted alias")\n' + 'x = 1\n' * 200
    run = await run_store.create_run('session-recover-alias', 'resume execute alias', panel_id='panel-recover-alias')
    await run_store.start_step(
        run['id'],
        'execute_code',
        {
            'attempt': 1,
            'mode': '3d',
            'code': long_code,
            'code_preview': long_code[:500],
            'output_formats': ['step'],
            'user_prompt': 'resume execute alias',
            'is_2d': False,
        },
    )

    recovered = await recover_running_runs()

    stored_run = await run_store.get_run(run['id'])
    stored_steps = await run_store.list_steps(run['id'])

    assert recovered == 1
    assert stored_run['status'] == 'blocked'
    assert stored_steps[0]['status'] == 'failed'
    assert stored_steps[-1]['step_type'] == 'resume_available'
    assert stored_steps[-1]['status'] == 'blocked'
    assert stored_steps[-1]['output']['next_step'] == 'execute_cad_code'
    assert stored_steps[-1]['output']['resume_input']['code'] == long_code


@pytest.mark.asyncio
async def test_recover_running_executing_code_alias_marks_resume_available(temp_db):
    code = 'print("interrupted generated execution")'
    run = await run_store.create_run('session-recover-executing', 'resume generated execution', panel_id='panel-recover-executing')
    await run_store.start_step(
        run['id'],
        'executing_code',
        {
            'code': code,
            'code_preview': code[:500],
            'output_formats': ['step', 'stl'],
            'user_prompt': 'resume generated execution',
            'is_2d': False,
        },
    )

    recovered = await recover_running_runs()

    stored_run = await run_store.get_run(run['id'])
    stored_steps = await run_store.list_steps(run['id'])

    assert recovered == 1
    assert stored_run['status'] == 'blocked'
    assert stored_steps[0]['status'] == 'failed'
    assert stored_steps[-1]['step_type'] == 'resume_available'
    assert stored_steps[-1]['status'] == 'blocked'
    assert stored_steps[-1]['output']['resume_input']['code'] == code



@pytest.mark.asyncio
async def test_failed_interrupted_repair_run_gets_resume_available(temp_db):
    run = await run_store.create_run('session-repair-interrupted', 'repair interrupted', panel_id='panel-repair-interrupted')
    execute_step = await run_store.start_step(
        run['id'],
        'execute_cad_code',
        {
            'attempt': 2,
            'mode': '3d',
            'code': 'print("last known good code")',
            'output_formats': ['step'],
            'user_prompt': 'repair interrupted',
            'is_2d': False,
        },
    )
    await run_store.complete_step(execute_step['id'], {'attempt': 2, 'mode': '3d', 'execution_time_ms': 10})
    repair_step = await run_store.start_step(run['id'], 'repair_code', {'attempt': 2, 'stage': 'vision'})
    await run_store.fail_step(
        repair_step['id'],
        {'type': 'InterruptedRun', 'message': 'interrupted while repairing'},
    )

    recovered = await ensure_run_resume_available(run['id'])

    stored_run = await run_store.get_run(run['id'])
    stored_steps = await run_store.list_steps(run['id'])
    assert recovered == 1
    assert stored_run['status'] == 'blocked'
    assert stored_steps[-1]['step_type'] == 'resume_available'
    assert stored_steps[-1]['status'] == 'blocked'
    assert stored_steps[-1]['output']['resume_input']['code'] == 'print("last known good code")'



@pytest.mark.asyncio
async def test_sanitize_terminal_run_with_running_step_marks_step_failed(temp_db):
    run = await run_store.create_run('session-sanitize', 'sanitize bad history', panel_id='panel-sanitize')
    step = await run_store.start_step(run['id'], 'execute_cad_code', {'attempt': 1})
    await run_store.complete_run(run['id'], status='failed')

    sanitized = await sanitize_incomplete_steps(run['id'])
    stored_run = await run_store.get_run(run['id'])
    stored_step = await run_store.get_step(step['id'])

    assert sanitized == 1
    assert stored_run['status'] == 'failed'
    assert stored_step['status'] == 'failed'
    assert stored_step['error']['type'] == 'IncompleteStep'


@pytest.mark.asyncio
async def test_sanitize_keeps_terminal_run_status(temp_db):
    run = await run_store.create_run('session-sanitize-success', 'sanitize success', panel_id='panel-sanitize')
    step = await run_store.start_step(run['id'], 'resume_available', {'next_step': 'execute_cad_code'})
    await run_store.complete_run(run['id'], status='succeeded')

    sanitized = await sanitize_incomplete_steps(run['id'])
    stored_run = await run_store.get_run(run['id'])
    stored_step = await run_store.get_step(step['id'])

    assert sanitized == 1
    assert stored_run['status'] == 'succeeded'
    assert stored_step['status'] == 'failed'
    assert stored_step['error']['type'] == 'IncompleteStep'


def test_classify_resume_state_after_successful_repair_is_resumable():
    decision = classify_resume_state([
        {
            'step_type': 'repair_code',
            'status': 'succeeded',
            'input': {'attempt': 1},
            'output': {'code_changed': True, 'repaired_code': 'print("fixed")'},
            'error': None,
        }
    ])

    assert decision['resumable'] is True
    assert decision['next_step'] == 'execute_cad_code'
    assert decision['input']['code'] == 'print("fixed")'
    assert decision['message'] == '\u4e0a\u6b21\u4efb\u52a1\u53ef\u7ee7\u7eed\uff1a\u81ea\u52a8\u4fee\u590d\u5df2\u5b8c\u6210\uff0c\u7b49\u5f85\u91cd\u65b0\u6267\u884c CAD \u4ee3\u7801\u3002'


def test_classify_resume_state_running_execute_step_is_resumable_with_full_code():
    decision = classify_resume_state([
        {
            'step_type': 'execute_cad_code',
            'status': 'running',
            'input': {
                'attempt': 1,
                'code': 'print("interrupted")',
                'output_formats': ['step'],
                'user_prompt': 'resume interrupted',
                'is_2d': False,
            },
            'output': None,
            'error': None,
        }
    ])

    assert decision['resumable'] is True
    assert decision['next_step'] == 'execute_cad_code'
    assert decision['input']['code'] == 'print("interrupted")'
    assert decision['message'] == '\u4e0a\u6b21\u4efb\u52a1\u6267\u884c\u4e2d\u65ad\uff0c\u53ef\u7ee7\u7eed\u91cd\u65b0\u6267\u884c CAD \u4ee3\u7801\u3002'
