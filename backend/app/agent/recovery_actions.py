from __future__ import annotations

from app.agent.design_brief import localize_user_question
from app.agent.failure_taxonomy import FixPath, classify
from app.models.schemas import GenerationResult, GenerateResponse, RecoveryAction


def _push_unique(actions: list[RecoveryAction], action: RecoveryAction) -> None:
    if not any(existing.action_type == action.action_type and existing.label == action.label for existing in actions):
        actions.append(action)


def _open_questions(result: GenerationResult | GenerateResponse) -> list[str]:
    brief = result.design_brief or (result.plan.design_brief if result.plan else None)
    return list(brief.open_questions) if brief and brief.open_questions else []


def build_recovery_actions(result: GenerationResult | GenerateResponse) -> list[RecoveryAction]:
    actions: list[RecoveryAction] = []
    questions = _open_questions(result)

    if result.needs_confirmation and questions:
        first_question = localize_user_question(questions[0])
        _push_unique(actions, RecoveryAction(
            label="\u56de\u7b54\u5f85\u786e\u8ba4\u9879",
            prompt=f"\u9488\u5bf9\u8bbe\u8ba1\u7b80\u62a5\u4e2d\u7684\u95ee\u9898\u8865\u5145\u8bf4\u660e\uff1a{first_question} \u6211\u7684\u56de\u7b54\u662f\uff1a",
            reason="\u9700\u8981\u8865\u5145\u5173\u952e\u8bbe\u8ba1\u4fe1\u606f\u540e\u624d\u80fd\u7ee7\u7eed\u751f\u6210 CAD \u6a21\u578b\u3002",
            action_type="clarify",
        ))

    inspect_report = result.inspect_report
    print_warnings = list(result.validation.print_warnings) if result.validation and result.validation.print_warnings else []
    if inspect_report and inspect_report.print_warnings:
        print_warnings.extend(inspect_report.print_warnings)
    if inspect_report and inspect_report.verdict in {"warn", "fail"}:
        warning_reason = "; ".join(print_warnings[:2]) or "\u68c0\u67e5\u62a5\u544a\u5b58\u5728\u8b66\u544a\u6216\u5931\u8d25\u9879\u3002"
        _push_unique(actions, RecoveryAction(
            label="\u4f18\u5316\u53ef\u6253\u5370\u6027",
            prompt="\u6309\u5f53\u524d\u5236\u9020\u914d\u7f6e\u4f18\u5316\u6a21\u578b\uff1a\u52a0\u539a\u58c1\u539a\u3001\u8fb9\u7f18\u5012\u5706\uff0c\u5e76\u51cf\u5c11\u65e0\u652f\u6491\u60ac\u5782\u3002",
            reason=warning_reason,
            action_type="fix_printability",
        ))

    if not result.success or result.repair_history:
        error_type = (result.error or {}).get("type", "GenerationError")
        error_message = (result.error or {}).get("message", "\u751f\u6210\u672a\u6b63\u5e38\u5b8c\u6210\u3002")
        failure = classify(error_type, error_message)
        if failure.fix_path is not FixPath.HARD_STOP:
            _push_unique(actions, RecoveryAction(
                label="\u7b80\u5316\u91cd\u8bd5",
                prompt="\u7528\u66f4\u7b80\u5355\u7a33\u5065\u7684\u51e0\u4f55\u65b9\u5f0f\u91cd\u65b0\u751f\u6210\uff0c\u540c\u65f6\u4fdd\u7559\u6838\u5fc3\u529f\u80fd\u3001\u5173\u952e\u5c3a\u5bf8\u548c\u5236\u9020\u914d\u7f6e\u3002",
                reason=f"{error_type}: {error_message}",
                action_type="retry_simpler",
            ))
        _push_unique(actions, RecoveryAction(
            label="\u89e3\u91ca\u5931\u8d25\u539f\u56e0",
            prompt="\u89e3\u91ca\u672c\u6b21\u751f\u6210\u4e3a\u4ec0\u4e48\u5931\u8d25\uff0c\u5e76\u5efa\u8bae\u6700\u5c0f\u7684\u63d0\u793a\u8bcd\u4fee\u6539\u65b9\u5f0f\u3002",
            reason="\u5e2e\u52a9\u7528\u6237\u5728\u91cd\u8bd5\u524d\u7406\u89e3\u5931\u8d25\u539f\u56e0\u3002",
            action_type="explain",
        ))

    if inspect_report and inspect_report.verdict == "fail":
        _push_unique(actions, RecoveryAction(
            label="\u67e5\u770b\u68c0\u67e5\u95ee\u9898",
            prompt="\u603b\u7ed3\u68c0\u67e5\u5931\u8d25\u9879\uff0c\u5e76\u5217\u51fa\u91cd\u65b0\u751f\u6210\u524d\u9700\u8981\u8c03\u6574\u7684\u5177\u4f53\u51e0\u4f55\u4fee\u6539\u3002",
            reason="\u68c0\u67e5\u672a\u901a\u8fc7\uff0c\u9700\u8981\u5728\u518d\u6b21\u6253\u5370\u524d\u5148\u590d\u6838\u95ee\u9898\u3002",
            action_type="inspect",
        ))

    return actions[:4]
