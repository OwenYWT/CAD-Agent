from app.agent.recovery_actions import build_recovery_actions
from app.models.schemas import (
    DesignBrief,
    GenerationResult,
    InspectCheck,
    InspectReport,
    RecoveryAction,
    RepairStep,
)


def test_recovery_action_schema_serializes_prompt_and_type():
    action = RecoveryAction(
        label="\u7b80\u5316\u91cd\u8bd5",
        prompt="\u7528\u66f4\u7b80\u5355\u7a33\u5065\u7684\u51e0\u4f55\u65b9\u5f0f\u91cd\u65b0\u751f\u6210\u3002",
        reason="Previous execution timed out.",
        action_type="retry_simpler",
    )

    payload = action.model_dump()

    assert payload["label"] == "\u7b80\u5316\u91cd\u8bd5"
    assert payload["action_type"] == "retry_simpler"
    assert "\u7b80\u5355" in payload["prompt"]


def test_build_recovery_actions_for_confirmation_questions():
    result = GenerationResult(
        success=False,
        needs_confirmation=True,
        design_brief=DesignBrief(
            intent_summary="Desk clamp",
            artifact_type="clamp",
            open_questions=["What desk thickness should this clamp fit?"],
        ),
        error={"type": "NeedsConfirmation", "message": "confirm details"},
    )

    actions = build_recovery_actions(result)

    assert actions[0].action_type == "clarify"
    assert actions[0].label == "\u56de\u7b54\u5f85\u786e\u8ba4\u9879"
    assert "桌面厚度" in actions[0].prompt


def test_build_recovery_actions_for_failed_execution_and_repair_history():
    result = GenerationResult(
        success=False,
        error={"type": "TimeoutError", "message": "execution timed out"},
        repair_history=[
            RepairStep(
                attempt=1,
                stage="execution",
                error_type="TimeoutError",
                message="execution timed out",
                action="simplify fillets",
                status="failed",
            )
        ],
    )

    actions = build_recovery_actions(result)

    assert [action.action_type for action in actions][:2] == ["retry_simpler", "explain"]
    assert "\u7b80\u5355" in actions[0].prompt


def test_build_recovery_actions_for_inspect_warning():
    result = GenerationResult(
        success=True,
        inspect_report=InspectReport(
            verdict="warn",
            is_watertight=True,
            volume=100,
            checks=[InspectCheck(name="Wall", status="warn", message="Thin wall", source="dfm")],
            print_warnings=["Wall thickness may be too thin"],
        ),
    )

    actions = build_recovery_actions(result)

    assert actions[0].action_type == "fix_printability"
    assert "Wall thickness may be too thin" in actions[0].reason
