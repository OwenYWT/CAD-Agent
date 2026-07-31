from app.agent.step_runner import NextStepDecision, decide_next_step


def step(step_type, status, *, output=None, error=None, input_data=None):
    return {
        "step_type": step_type,
        "status": status,
        "input": input_data or {},
        "output": output,
        "error": error,
    }


def test_failed_execute_step_routes_to_repair_code():
    decision = decide_next_step([
        step(
            "execute_cad_code",
            "failed",
            error={"type": "SyntaxError", "message": "invalid syntax"},
            input_data={"attempt": 1},
        )
    ])

    assert decision == NextStepDecision(
        step_type="repair_code",
        reason="execute_failed",
        input_data={
            "attempt": 1,
            "stage": "execution",
            "error_type": "SyntaxError",
            "message": "invalid syntax",
        },
    )


def test_successful_repair_routes_to_execute_retry():
    decision = decide_next_step([
        step("execute_cad_code", "failed", input_data={"attempt": 1}),
        step(
            "repair_code",
            "succeeded",
            output={"stage": "execution", "code_changed": True, "repaired_code": 'print("fixed")'},
            input_data={"attempt": 1, "output_formats": ["step"], "request_id": "req-1"},
        ),
    ])

    assert decision == NextStepDecision(
        step_type="execute_cad_code",
        reason="repair_succeeded",
        input_data={
            "attempt": 2,
            "source": "repair_code",
            "code": 'print("fixed")',
            "output_formats": ["step"],
            "request_id": "req-1",
        },
    )


def test_successful_execute_routes_to_finalize_result():
    decision = decide_next_step([
        step("execute_cad_code", "succeeded", output={"attempt": 2, "mode": "3d"})
    ])

    assert decision == NextStepDecision(
        step_type="finalize_result",
        reason="execute_succeeded",
        input_data={"success": True, "attempt": 2},
    )


def test_terminal_or_running_step_has_no_next_step():
    assert decide_next_step([step("finalize_result", "succeeded")]).step_type is None
    assert decide_next_step([step("execute_cad_code", "running")]).step_type is None


def test_repair_without_code_change_routes_to_failed_finalize():
    decision = decide_next_step([
        step("execute_cad_code", "failed", input_data={"attempt": 1}),
        step(
            "repair_code",
            "succeeded",
            output={"stage": "execution", "code_changed": False},
            input_data={"attempt": 1},
        ),
    ])

    assert decision == NextStepDecision(
        step_type="finalize_result",
        reason="repair_no_change",
        input_data={"success": False, "message": "\u4fee\u590d\u540e\u4ee3\u7801\u672a\u53d1\u751f\u53d8\u5316"},
    )
