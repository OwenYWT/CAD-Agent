import pytest
from app.freecad.contracts import FreeCADPlanningError, decode_planner_response
from app.workflows.errors import planning_error

@pytest.mark.parametrize('reason,code',[
    ('unsupported: operation not available','freecad_capability_unsupported'),
    ('needs_clarification: countersink standard missing','engineering_input_required'),
])
def test_deliberate_planner_rejection_is_not_malformed_json(reason,code):
    import json
    with pytest.raises(FreeCADPlanningError) as caught:
        decode_planner_response(json.dumps({'error':reason}))
    assert caught.value.code==code
    error=planning_error(caught.value)
    assert error.type==code and error.non_retryable
    assert reason in str(error)
