"""Deterministic developer-facing summaries for live evaluation reports."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


_EVENT_LABELS = {
    "session_started": "任务已接收",
    "requirement_parsed": "需求已解析",
    "obligations_bound": "需求已绑定验收条件",
    "plan_created": "执行计划已生成",
    "operation_finished": "操作已完成",
    "geometry_validation_finished": "几何校验",
    "visual_validation_finished": "视觉校验",
    "dfm_validation_finished": "制造性校验",
    "bom_validation_finished": "物料清单校验",
    "candidate_sealed": "候选版本已封存",
    "change_set_created": "变更集已创建",
    "change_set_reviewed": "变更集已审阅",
    "change_set_rejected": "变更集已拒绝",
    "revision_committed": "版本已提交",
    "revision_rolled_back": "版本已回滚",
    "operation_failed": "执行失败",
    "session_finished": "任务已结束",
}

_STATUS_LABELS = {
    "pass": "通过",
    "fail": "未通过",
    "error": "错误",
    "blocked": "阻断",
    "skipped": "跳过",
    "unknown": "未确定",
    "running": "执行中",
    "building": "执行中",
    "finished": "已完成",
    "failed": "未通过",
    "passed": "通过",
    "success": "通过",
    "succeeded": "通过",
    "cancelled": "已取消",
    "rendered": "已渲染",
    "accepted": "已接受",
    "committed": "已提交",
}

_METRIC_UNITS = {
    "bridge_span_mm": "mm",
    "face_count": "",
    "horizontal_roof_count": "",
    "is_watertight": "",
    "material_ratio": "",
    "max_size_mm": "mm",
    "min_feature_size_mm": "mm",
    "min_wall_thickness_mm": "mm",
    "overhang_ratio": "",
    "surface_area_mm2": "mm^2",
    "unanchored_roof_area_mm2": "mm^2",
    "volume_mm3": "mm^3",
}


def _status(event: Mapping[str, Any]) -> str:
    payload = event.get("payload")
    payload_status = payload.get("status") if isinstance(payload, Mapping) else None
    event_status = str(event.get("status") or "unknown")
    value = str(payload_status or event_status)
    if value == "completed" and event_status != "unknown":
        value = event_status
    return _STATUS_LABELS.get(value, value)


def _failure_detail(event: Mapping[str, Any]) -> str:
    payload = event.get("payload")
    if not isinstance(payload, Mapping):
        return ""
    error_type = str(payload.get("error_type") or "Error")
    error_message = str(payload.get("error_message") or "unknown failure")
    return f": {error_type}: {error_message}"


def _timeline(report: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    timeline = report.get("timeline")
    if not isinstance(timeline, list):
        return []
    return [event for event in timeline if isinstance(event, Mapping)]


def _events(report: Mapping[str, Any], event_type: str) -> list[Mapping[str, Any]]:
    return [event for event in _timeline(report) if event.get("event_type") == event_type]


def _event_result(event: Mapping[str, Any]) -> str:
    status = str(event.get("status") or "unknown")
    if status in {"pass", "fail"}:
        return status
    payload = event.get("payload")
    if not isinstance(payload, Mapping):
        return "unknown"
    outcome = str(payload.get("outcome") or "").lower()
    if outcome in {"pass", "passed", "success", "succeeded"}:
        return "pass"
    if outcome in {"fail", "failed", "error", "rejected"}:
        return "fail"
    return "unknown"


def _evidence_ids(report: Mapping[str, Any]) -> set[str]:
    evidence = report.get("evidence")
    if not isinstance(evidence, list):
        return set()
    return {
        str(item["evidence_id"])
        for item in evidence
        if isinstance(item, Mapping) and item.get("evidence_id")
    }


def _event_has_evidence(event: Mapping[str, Any], available: set[str]) -> bool:
    event_ids = event.get("evidence_ids")
    ids = {str(item) for item in event_ids} if isinstance(event_ids, list) else set()
    payload = event.get("payload")
    if isinstance(payload, Mapping) and payload.get("evidence_id"):
        ids.add(str(payload["evidence_id"]))
    return bool(ids & available)


def _validation_result(report: Mapping[str, Any], event_type: str) -> str:
    events = _events(report, event_type)
    if not events:
        return "未记录"
    available = _evidence_ids(report)
    for event in reversed(events):
        result = _event_result(event)
        if result == "fail":
            return "未通过"
        if result == "pass":
            return "通过" if _event_has_evidence(event, available) else "未确定"
    return "未确定"


def _payload_status(event: Mapping[str, Any]) -> str:
    payload = event.get("payload")
    if not isinstance(payload, Mapping):
        return ""
    return str(payload.get("status") or "").lower()


def _latest_payload_status(report: Mapping[str, Any], event_type: str) -> str:
    events = _events(report, event_type)
    return _payload_status(events[-1]) if events else ""


def _format_number(value: Any) -> str:
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return f"{value:.12g}"
    return str(value)


def _format_measurement(value: Any, unit: str = "") -> str:
    if value is None:
        return "未记录"
    formatted = _format_number(value)
    return f"{formatted} {unit}".rstrip() if unit else formatted


def _evidence_by_id(report: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    evidence = report.get("evidence")
    if not isinstance(evidence, list):
        return {}
    return {
        str(item["evidence_id"]): item
        for item in evidence
        if isinstance(item, Mapping) and item.get("evidence_id")
    }


def _observed_evidence(
    report: Mapping[str, Any], event: Mapping[str, Any]
) -> list[tuple[str, Mapping[str, Any]]]:
    evidence_by_id = _evidence_by_id(report)
    event_ids = event.get("evidence_ids")
    if not isinstance(event_ids, list):
        return []
    result = []
    for evidence_id in event_ids:
        evidence = evidence_by_id.get(str(evidence_id))
        observed = evidence.get("observed") if isinstance(evidence, Mapping) else None
        if isinstance(observed, Mapping):
            result.append((str(evidence_id), observed))
    return result


def _geometry_events_with_evidence(
    report: Mapping[str, Any],
) -> list[tuple[Mapping[str, Any], str, Mapping[str, Any]]]:
    result = []
    for event in _events(report, "geometry_validation_finished"):
        for evidence_id, observed in _observed_evidence(report, event):
            result.append((event, evidence_id, observed))
    return result


def _render_geometry_quantitative(report: Mapping[str, Any]) -> list[str]:
    rows = _geometry_events_with_evidence(report)
    lines = ["## 几何量化评测", ""]
    if not rows:
        lines.append("- 没有可展示的几何量化证据。")
        return lines

    for index, (event, evidence_id, observed) in enumerate(rows, start=1):
        result = "通过" if _event_result(event) == "pass" else (
            "未通过" if _event_result(event) == "fail" else "未确定"
        )
        lines.append(f"- 第 {index} 次验证: {result}（证据: {evidence_id}）")
        acceptance = observed.get("acceptance")
        checks = acceptance.get("evidence") if isinstance(acceptance, Mapping) else None
        if isinstance(checks, list) and checks:
            for check in checks:
                if not isinstance(check, Mapping):
                    continue
                check_id = str(check.get("check_id") or "未记录")
                details = check.get("details")
                details = details if isinstance(details, Mapping) else {}
                comparison = details.get("comparison") or check.get("comparison")
                comparison = comparison if isinstance(comparison, Mapping) else {}
                unit = str(comparison.get("unit") or "")
                target = _format_measurement(comparison.get("value"), unit)
                measured = details.get("measured") or check.get("measured")
                measured_value = measured[0] if isinstance(measured, list) and measured else None
                actual = _format_measurement(measured_value, unit)
                check_result = _STATUS_LABELS.get(
                    str(check.get("outcome") or "unknown"), "未确定"
                )
                lines.append(
                    f"  - 检查 {check_id}: 目标 {target}; 实测 {actual}; 结果 {check_result}"
                )
        else:
            lines.append("  - 实测值: 未记录")

        tolerance = observed.get("dimension_tolerance")
        if tolerance is not None:
            lines.append(f"  - 尺寸容差: {_format_measurement(tolerance, 'mm')}")
        artifacts = observed.get("artifacts")
        if isinstance(artifacts, list):
            for artifact in artifacts:
                if not isinstance(artifact, Mapping):
                    continue
                filename = str(artifact.get("filename") or "未记录")
                dimensions = artifact.get("dimensions_mm")
                if isinstance(dimensions, list) and dimensions:
                    dimensions_text = " x ".join(_format_number(item) for item in dimensions)
                    dimensions_text += " mm"
                else:
                    dimensions_text = "未记录"
                lines.append(
                    f"  - 工件 {filename}: 尺寸 {dimensions_text}; "
                    f"实体数 {_format_measurement(artifact.get('solid_count'))}; "
                    f"体积 {_format_measurement(artifact.get('volume_mm3'), 'mm^3')}"
                )
    return lines


def _dfm_evidence(report: Mapping[str, Any]) -> list[tuple[str, Mapping[str, Any]]]:
    evidence_by_id = _evidence_by_id(report)
    result = []
    for event in _events(report, "dfm_validation_finished"):
        for evidence_id, observed in _observed_evidence(report, event):
            result.append((evidence_id, observed))
    if result:
        return result
    return [
        (evidence_id, evidence["observed"])
        for evidence_id, evidence in evidence_by_id.items()
        if evidence.get("source_kind") == "agent_v2.validate_dfm"
        and isinstance(evidence.get("observed"), Mapping)
    ]


def _render_dfm_quantitative(report: Mapping[str, Any]) -> list[str]:
    rows = _dfm_evidence(report)
    lines = ["## DFM 量化评测", ""]
    if not rows:
        lines.append("- 没有可展示的 DFM 量化证据。")
        return lines
    for evidence_id, observed in rows:
        outcome = _STATUS_LABELS.get(str(observed.get("outcome") or "unknown"), "未确定")
        lines.append(f"- 验证结果: {outcome}（证据: {evidence_id}）")
        if observed.get("process") is not None:
            lines.append(f"- 过程: {observed['process']}")
        metrics = observed.get("metrics")
        if isinstance(metrics, Mapping) and metrics:
            lines.append("- 指标:")
            for name, value in metrics.items():
                unit = _METRIC_UNITS.get(str(name), "")
                lines.append(f"  - {name}: {_format_measurement(value, unit)}")
        else:
            lines.append("- 指标: 未记录")
        evaluated = observed.get("evaluated_rule_ids")
        if isinstance(evaluated, list):
            evaluated_ids = {str(item) for item in evaluated}
            policy = observed.get("policy_object")
            rules = policy.get("rules") if isinstance(policy, Mapping) else None
            if isinstance(rules, list) and rules:
                violations = observed.get("violations")
                violation_ids = {
                    str(item.get("rule_id") or item.get("id"))
                    for item in violations
                    if isinstance(item, Mapping)
                    and (item.get("rule_id") or item.get("id"))
                } if isinstance(violations, list) else set()
                for rule in rules:
                    if not isinstance(rule, Mapping) or not rule.get("id"):
                        continue
                    rule_id = str(rule["id"])
                    state = "已评测" if rule_id in evaluated_ids else "未评测"
                    if rule_id in violation_ids:
                        state = "发现违规"
                    threshold = []
                    unit = str(rule.get("unit") or "")
                    if rule.get("threshold_min") is not None:
                        threshold.append(
                            f">= {_format_measurement(rule['threshold_min'], unit)}"
                        )
                    if rule.get("threshold_max") is not None:
                        threshold.append(
                            f"<= {_format_measurement(rule['threshold_max'], unit)}"
                        )
                    threshold_text = "; 阈值 " + " and ".join(threshold) if threshold else ""
                    lines.append(f"- 规则 {rule_id}: {state}{threshold_text}")
            else:
                rule_text = ", ".join(str(item) for item in evaluated) or "无"
                lines.append(f"- 评测规则: {rule_text}")
        violations = observed.get("violations")
        if isinstance(violations, list):
            lines.append(f"- 违规: {len(violations)}")
        unevaluated = observed.get("unevaluated_rule_ids")
        if isinstance(unevaluated, list) and unevaluated:
            lines.append(f"- 未评测规则: {', '.join(str(item) for item in unevaluated)}")
    return lines


def _render_repair_comparison(report: Mapping[str, Any]) -> list[str]:
    repairs = [
        str((event.get("payload") or {}).get("activity"))
        for event in _timeline(report)
        if str((event.get("payload") or {}).get("activity") or "").startswith("agent_v2.repair_")
    ]
    rows = _geometry_events_with_evidence(report)
    lines = ["## 修复前后对比", ""]
    if not repairs:
        lines.append("- 未记录明确的修复活动。")
        return lines
    lines.append(f"- 修复活动: {', '.join(repairs)}")
    if len(rows) < 2:
        lines.append("- 修复前后几何量化证据: 未完整记录")
        return lines
    before = rows[0][2]
    after = rows[-1][2]
    before_result = _STATUS_LABELS.get(str(before.get("outcome") or "unknown"), "未确定")
    after_result = _STATUS_LABELS.get(str(after.get("outcome") or "unknown"), "未确定")
    lines.append(f"- 修复前结果: {before_result}（证据: {rows[0][1]}）")
    lines.append(f"- 修复后结果: {after_result}（证据: {rows[-1][1]}）")
    return lines


def _render_stage_duration(report: Mapping[str, Any]) -> list[str]:
    lines = ["## \u9636\u6bb5\u8017\u65f6", ""]
    durations = []
    for event in _timeline(report):
        payload = event.get("payload")
        if not isinstance(payload, Mapping):
            continue
        duration = payload.get("duration_ms")
        activity = payload.get("activity")
        if isinstance(duration, (int, float)) and not isinstance(duration, bool) and activity:
            durations.append((str(activity), max(0, int(duration))))
    if not durations:
        lines.append("- \u672a\u8bb0\u5f55\u771f\u5b9e\u9636\u6bb5\u8017\u65f6")
        return lines
    for activity, duration in durations:
        lines.append(f"- {activity}: {duration} ms")
    metrics = report.get("metrics")
    total = metrics.get("total_duration_ms") if isinstance(metrics, Mapping) else None
    if not isinstance(total, (int, float)) or isinstance(total, bool):
        total = sum(duration for _, duration in durations)
    lines.append(f"- \u603b\u8017\u65f6: {max(0, int(total))} ms")
    return lines


def _geometry_check_measurements(observed: Mapping[str, Any]) -> dict[str, tuple[float, float, str]]:
    acceptance = observed.get("acceptance")
    checks = acceptance.get("evidence") if isinstance(acceptance, Mapping) else None
    if not isinstance(checks, list):
        return {}
    measurements = {}
    for check in checks:
        if not isinstance(check, Mapping) or not check.get("check_id"):
            continue
        details = check.get("details")
        details = details if isinstance(details, Mapping) else {}
        comparison = details.get("comparison") or check.get("comparison")
        comparison = comparison if isinstance(comparison, Mapping) else {}
        measured = details.get("measured") or check.get("measured")
        measured = measured[0] if isinstance(measured, list) and measured else measured
        target = comparison.get("value")
        if isinstance(target, (int, float)) and not isinstance(target, bool) and isinstance(measured, (int, float)) and not isinstance(measured, bool):
            measurements[str(check["check_id"])] = (float(target), float(measured), str(comparison.get("unit") or ""))
    return measurements


def _render_repair_benefit(report: Mapping[str, Any]) -> list[str]:
    lines = ["## \u4fee\u590d\u6536\u76ca", ""]
    rows = _geometry_events_with_evidence(report)
    repairs = [
        event for event in _timeline(report)
        if str((event.get("payload") or {}).get("activity") or "").startswith("agent_v2.repair_")
    ]
    if not repairs or len(rows) < 2:
        lines.append("- \u672a\u8bb0\u5f55\u53ef\u8ba1\u7b97\u7684\u771f\u5b9e\u4fee\u590d\u6536\u76ca")
        return lines
    before_measurements = _geometry_check_measurements(rows[0][2])
    after_measurements = _geometry_check_measurements(rows[-1][2])
    common_checks = [check_id for check_id in before_measurements if check_id in after_measurements]
    if not common_checks:
        lines.append("- \u672a\u8bb0\u5f55\u53ef\u8ba1\u7b97\u7684\u771f\u5b9e\u4fee\u590d\u6536\u76ca")
        return lines
    for check_id in common_checks:
        before_target, before_value, unit = before_measurements[check_id]
        after_target, after_value, after_unit = after_measurements[check_id]
        if before_target != after_target or unit != after_unit:
            continue
        before_error = abs(before_target - before_value)
        after_error = abs(after_target - after_value)
        target_text = _format_measurement(before_target, unit)
        lines.append(
            f"- \u4fee\u590d\u524d: \u76ee\u6807 {target_text}; \u5b9e\u6d4b {_format_measurement(before_value, unit)}; \u8bef\u5dee {_format_measurement(before_error, unit)}"
        )
        lines.append(
            f"- \u4fee\u590d\u540e: \u76ee\u6807 {target_text}; \u5b9e\u6d4b {_format_measurement(after_value, unit)}; \u8bef\u5dee {_format_measurement(after_error, unit)}"
        )
    if len(lines) == 2:
        lines.append("- \u672a\u8bb0\u5f55\u53ef\u8ba1\u7b97\u7684\u771f\u5b9e\u4fee\u590d\u6536\u76ca")
    return lines


def _render_tool_call_summary(report: Mapping[str, Any]) -> list[str]:
    calls = report.get("tool_calls")
    calls = calls if isinstance(calls, list) else []
    metrics = report.get("metrics")
    metrics = metrics if isinstance(metrics, Mapping) else {}
    consistency = report.get("consistency")
    consistency = consistency if isinstance(consistency, Mapping) else {}
    lines = ["## \u5de5\u5177\u8c03\u7528\u7edf\u8ba1", ""]
    lines.append(f"- \u603b\u8c03\u7528: {int(metrics.get('total_call_count') or len(calls))}")
    by_kind = {}
    if isinstance(consistency.get("by_kind"), Mapping):
        by_kind = consistency["by_kind"]
    if not by_kind:
        for call in calls:
            if isinstance(call, Mapping):
                kind = str(call.get("call_kind") or "unknown")
                by_kind[kind] = int(by_kind.get(kind) or 0) + 1
    for kind, count in sorted(by_kind.items()):
        lines.append(f"- {kind}: {count}")
    if metrics.get("failed_call_count") is not None:
        lines.append(f"- \u5931\u8d25\u8c03\u7528: {int(metrics.get('failed_call_count') or 0)}")

    lines.extend(["", "## \u8c03\u7528\u5e8f\u5217", ""])
    if not calls:
        lines.append("- \u672a\u8bb0\u5f55\u5de5\u5177\u8c03\u7528")
    else:
        for index, call in enumerate(calls, start=1):
            if not isinstance(call, Mapping):
                continue
            lines.append(
                f"{index}. {call.get('call_kind') or 'unknown'} / "
                f"{call.get('name') or 'unknown'} / {call.get('status') or 'unknown'}"
            )

    lines.extend(["", "## \u8c03\u7528\u4e00\u81f4\u6027", ""])
    unknown = "\u672a\u8bb0\u5f55"
    lines.append(
        f"- \u8c03\u7528\u5e8f\u5217\u7b7e\u540d: {consistency.get('sequence_signature') or unknown}"
    )
    lines.append(
        f"- \u7ed3\u6784\u7b7e\u540d: {consistency.get('structural_signature') or unknown}"
    )
    return lines


def _repair_summary(report: Mapping[str, Any]) -> str:
    repairs = []
    for event in _timeline(report):
        payload = event.get("payload")
        activity = str(payload.get("activity") or "") if isinstance(payload, Mapping) else ""
        if activity.startswith("agent_v2.repair_"):
            repairs.append(event)
    if repairs:
        return f"是，{len(repairs)} 次"
    return "未确定"


def _engineering_summary(report: Mapping[str, Any]) -> list[str]:
    timeline = _timeline(report)
    failures = [event for event in timeline if event.get("event_type") == "operation_failed"]
    requirement_failures = [
        event
        for event in failures
        if str((event.get("payload") or {}).get("activity") or "")
        == "agent_v2.requirements"
    ]
    requirement_events = _events(report, "requirement_parsed")
    requirement_result = "失败" if requirement_failures else (
        "已记录" if requirement_events else "未记录"
    )

    lines = ["## 工程过程结论", ""]
    lines.append(f"- 需求解析: {requirement_result}")
    final_status = str(report.get("status") or "running")
    if failures or final_status in {"failed", "blocked", "error", "cancelled"}:
        failure = failures[0] if failures else None
        if failure is None:
            lines.extend(
                [
                    "- 失败阶段: 执行阶段",
                    "- 失败类型: 工作流未完成",
                    "- 是否进入建模: 未确定",
                    "- 最终结论: 任务未完成",
                ]
            )
            return lines
        payload = failure.get("payload")
        activity = str(payload.get("activity") or "") if isinstance(payload, Mapping) else ""
        message = str(payload.get("error_message") or "") if isinstance(payload, Mapping) else ""
        failure_stage = "需求核验" if activity == "agent_v2.requirements" else "执行阶段"
        failure_type = (
            "工程条件无法可靠核验"
            if "engineering_verification_unavailable" in message
            else str(payload.get("error_type") or "执行失败")
            if isinstance(payload, Mapping)
            else "执行失败"
        )
        lines.append(f"- 失败阶段: {failure_stage}")
        lines.append(f"- 失败类型: {failure_type}")
        lines.append("- 是否进入建模: 否" if failure_stage == "需求核验" else "- 是否进入建模: 未确定")
        lines.append("- 最终结论: 任务未完成")
        return lines

    lines.extend(
        [
            f"- 几何结果: {_validation_result(report, 'geometry_validation_finished')}",
            f"- 视觉结果: {'已渲染' if _latest_payload_status(report, 'visual_validation_finished') == 'rendered' else '未确定'}",
            f"- 制造性结果: {_validation_result(report, 'dfm_validation_finished')}",
            f"- 是否发生修复: {_repair_summary(report)}",
            f"- 候选版本: {'已封存' if _latest_payload_status(report, 'candidate_sealed') == 'succeeded' else '未确定'}",
            f"- 变更审阅: {'已接受' if _latest_payload_status(report, 'change_set_reviewed') == 'accepted' else '未确定'}",
            f"- 版本提交: {'已完成' if _latest_payload_status(report, 'revision_committed') == 'committed' else '未确定'}",
        ]
    )
    committed = _latest_payload_status(report, "revision_committed") == "committed"
    if final_status == "finished" and committed:
        conclusion = "任务完成"
    elif final_status == "finished":
        conclusion = "流程已结束，但未确认版本提交"
    elif final_status == "running":
        conclusion = "任务执行中"
    else:
        conclusion = "任务未完成"
    lines.append(f"- 最终结论: {conclusion}")
    return lines


def render_human_report(report: Mapping[str, Any]) -> str:
    """Render only facts already present in the live report."""

    lines = [
        "# CAD-Agent 执行过程报告",
        "",
        f"- 评测会话: {report.get('evaluation_run_id') or '未记录'}",
        f"- 工作流: {report.get('workflow_id') or '未记录'}",
        f"- 最终状态: {_STATUS_LABELS.get(str(report.get('status') or 'running'), str(report.get('status') or 'running'))}",
    ]
    source = report.get("source")
    if isinstance(source, Mapping):
        lines.append(
            f"- 来源: {source.get('kind') or '未记录'}"
            + (f" ({source['id']})" if source.get("id") else "")
        )
    lines.extend(["", "## 任务", ""])

    objective = ""
    timeline = report.get("timeline")
    if isinstance(timeline, list):
        for event in timeline:
            if not isinstance(event, Mapping) or event.get("event_type") != "session_started":
                continue
            payload = event.get("payload")
            if isinstance(payload, Mapping) and payload.get("objective"):
                objective = str(payload["objective"])
                break
    lines.append(f"任务目标: {objective or '未记录'}")
    lines.extend(["", "## 实际执行过程", ""])

    if not isinstance(timeline, list) or not timeline:
        lines.append("- 尚未记录工作流事件。")
    else:
        for event in timeline:
            if not isinstance(event, Mapping):
                continue
            event_type = str(event.get("event_type") or "operation_finished")
            label = _EVENT_LABELS.get(event_type, event_type.replace("_", " "))
            detail = _failure_detail(event) if event_type == "operation_failed" else ""
            lines.append(f"- {label}: {_status(event)}{detail}")

    lines.extend(
        [
            "",
            *_engineering_summary(report),
            "",
            *_render_geometry_quantitative(report),
            "",
            *_render_dfm_quantitative(report),
            "",
            *_render_stage_duration(report),
            "",
            *_render_repair_comparison(report),
            "",
            *_render_repair_benefit(report),
            "",
            *_render_tool_call_summary(report),
            "",
            "## 结果说明",
            "",
        ]
    )
    final_status = str(report.get("status") or "running")
    if final_status == "finished":
        lines.append("工作流已到达结束状态。以上结论仅依据已记录的真实执行事件。")
    elif final_status == "running":
        lines.append("工作流仍在执行，当前报告不是最终报告。")
    elif final_status == "cancelled":
        lines.append("工作流在成功完成前被取消。")
    else:
        lines.append(f"工作流结束状态为：{_STATUS_LABELS.get(final_status, final_status)}。")

    errors = report.get("errors")
    if isinstance(errors, list) and errors:
        lines.extend(["", "## 报告记录错误", ""])
        lines.extend(f"- {error}" for error in errors)
    return "\n".join(lines) + "\n"
