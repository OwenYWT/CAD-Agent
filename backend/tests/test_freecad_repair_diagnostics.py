from temporalio.exceptions import ApplicationError

from app.agent.durable_repair import decide_repair
from app.workflows.agent_v2 import McadAgentWorkflowV2

import pytest


def test_workflow_preserves_native_diagnostic_context():
    detail = {
        'execution_attempt_id': 'attempt', 'category': 'validation',
        'error_code': 'sketch_redundant_constraints', 'error_message': 'solver diagnosis',
        'runtime_error_type': None, 'operation_id': 'link-edges',
        'action': 'sketch.add_constraint',
        'details': {'object': 'Profile', 'solver_status': -2},
        'evidence': {'runtime_image': 'immutable-image'},
    }
    failure = McadAgentWorkflowV2._execution_failure(ApplicationError('failed', detail))
    assert failure == detail


@pytest.mark.parametrize('code', ['sketch_redundant_constraints',
    'sketch_conflicting_constraints', 'sketch_under_constrained'])
def test_structured_sketch_diagnosis_uses_targeted_repair(code):
    decision = decide_repair(category='validation', error_code=code,
        error_message='native diagnostic without English keywords', runtime_error_type=None,
        repair_count=0, seen_signatures=())
    assert decision.repairable
    assert decision.failure_class == code


def test_real_protocol_failure_never_enters_constraint_repair():
    decision = decide_repair(category='infrastructure', error_code='sandbox_protocol_error',
        error_message='sketch_redundant_constraints in diagnostic logs', runtime_error_type=None,
        repair_count=0, seen_signatures=())
    assert not decision.repairable


def test_distinct_constraint_operations_are_not_mistaken_for_a_repair_loop():
    values=dict(category='validation',error_code='sketch_redundant_constraints',
        error_message='Sketch Profile: redundant constraint 25',runtime_error_type=None)
    first=decide_repair(**values,operation_id='width-relation',repair_count=0,seen_signatures=())
    next_error=decide_repair(**values,operation_id='height-relation',repair_count=1,
        seen_signatures=(first.signature,))
    repeated=decide_repair(**values,operation_id='width-relation',repair_count=1,
        seen_signatures=(first.signature,))
    assert next_error.repairable
    assert not repeated.repairable
    assert repeated.strategy=='repeated_error_stop'
