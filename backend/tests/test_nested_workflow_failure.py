from temporalio.exceptions import ApplicationError, ActivityError, ChildWorkflowError
from app.workflows.agent_v2 import McadAgentWorkflowV2


def test_nested_child_failure_preserves_root_code_message_and_diagnostics():
    root = ApplicationError('invalid measurement at $.payload.base_state',
        {'category': 'internal', 'details': {'path': '$.payload.base_state'}},
        type='IntegerDomainError', non_retryable=True)
    activity = ActivityError('Activity task failed', scheduled_event_id=1, started_event_id=2,
        identity='worker', activity_type='model_jobs.submit', activity_id='submit', retry_state=None)
    activity.__cause__ = root
    child = ChildWorkflowError('Child Workflow execution failed', namespace='test',
        workflow_id='child', run_id='run', workflow_type='ModelJobWorkflow',
        initiated_event_id=1, started_event_id=2, retry_state=None)
    child.__cause__ = activity
    failure = McadAgentWorkflowV2._execution_failure(child)
    assert failure is not None
    assert failure['error_code'] == 'IntegerDomainError'
    assert '$.payload.base_state' in failure['error_message']
    assert failure['details']['path'] == '$.payload.base_state'
