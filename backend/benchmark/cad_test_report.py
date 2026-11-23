"""Render a readable, developer-only HTML view of a CADTest batch report."""

from __future__ import annotations

import html
import json
from collections import Counter
from pathlib import Path
from typing import Any, Mapping, Sequence


_STATUS_LABELS = {
    "passed": "通过",
    "pass": "通过",
    "finished": "已完成",
    "succeeded": "成功",
    "failed": "失败",
    "fail": "失败",
    "blocked": "阻断",
    "error": "错误",
    "evaluation_error": "评测错误",
    "measured": "已记录",
    "partial": "部分记录",
    "unknown": "状态未提供",
    "unavailable": "不可用",
    "not_applicable": "不适用",
    "running": "执行中",
}


def _escape(value: Any, default: str = "未记录") -> str:
    if value is None or value == "":
        return default
    return html.escape(str(value))


def _status(value: Any) -> str:
    if value is None or value == "":
        return "未记录"
    text = str(value)
    return _STATUS_LABELS.get(text.lower(), text)


def _status_class(value: Any) -> str:
    normalized = str(value or "unknown").lower().replace("_", "-")
    if normalized in {"passed", "pass", "succeeded", "finished"}:
        return "status-pass"
    if normalized in {"failed", "fail", "error", "evaluation-error"}:
        return "status-fail"
    if normalized in {"measured", "partial", "running"}:
        return "status-warn"
    return "status-muted"


def _status_cell(value: Any) -> str:
    return (
        f'<span class="status {_status_class(value)}">{_escape(_status(value))}</span>'
    )


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _format_duration(value: Any) -> str:
    number = _number(value)
    if number is None:
        return "未记录"
    if number >= 1000:
        return f"{number / 1000:.2f} s"
    return f"{number:.0f} ms"


def _format_percent(value: Any) -> str:
    number = _number(value)
    if number is None:
        return "未记录"
    return f"{number * 100:.1f}%"


def _format_count(value: Any) -> str:
    number = _number(value)
    if number is None:
        return "未记录"
    return f"{number:.0f}"


def _metric_value_html(value: str) -> str:
    text = str(value)
    main, separator, detail = text.partition("（")
    if not separator:
        return html.escape(text)
    return (
        f'<span class="metric-main">{html.escape(main)}</span>'
        f'<span class="metric-detail">（{html.escape(detail)}</span>'
    )


def _rows(report: Mapping[str, Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for case in report.get("cases", ()) or ():
        if not isinstance(case, Mapping):
            continue
        for run in case.get("runs", ()) or ():
            if not isinstance(run, Mapping):
                continue
            result.append({"case": case, "run": run})
    return result


def _case_entries(report: Mapping[str, Any]):
    for case_index, case in enumerate(report.get("cases", ()) or ()):
        if not isinstance(case, Mapping):
            continue
        runs = [run for run in case.get("runs", ()) or () if isinstance(run, Mapping)]
        if runs:
            yield case_index, case, runs


def _run_ratio(
    runs: Sequence[Mapping[str, Any]],
    getter,
    *,
    measured: set[str],
    success: set[str],
) -> str:
    statuses = [str(getter(run) or "unavailable").casefold() for run in runs]
    return _ratio_text(statuses, success=success)


def _run_process_status(run: Mapping[str, Any]) -> str:
    process = run.get("process_evaluation")
    return process.get("status") if isinstance(process, Mapping) else None


def _run_quality_status(run: Mapping[str, Any], key: str) -> str:
    process = run.get("process_evaluation")
    return process.get(key) if isinstance(process, Mapping) else None


def _run_cad_test_status(run: Mapping[str, Any]) -> str:
    return _cad_test_run_status(run)


def _run_artifact_status(run: Mapping[str, Any]) -> str:
    gate = run.get("artifact_gate")
    return _artifact_gate_status(gate) if isinstance(gate, Mapping) else "unavailable"


def _process_value(run: Mapping[str, Any], key: str) -> Any:
    process = run.get("process_evaluation")
    if isinstance(process, Mapping) and key in process:
        return process.get(key)
    live = run.get("live_evaluation")
    metrics = live.get("metrics") if isinstance(live, Mapping) else None
    return metrics.get(key) if isinstance(metrics, Mapping) else None


def _average(rows: Sequence[Mapping[str, Any]], key: str) -> float | None:
    values = [_number(_process_value(item["run"], key)) for item in rows]
    values = [value for value in values if value is not None]
    return sum(values) / len(values) if values else None


def _status_metric(
    rows: Sequence[Mapping[str, Any]],
    getter,
    *,
    measured: set[str],
    success: set[str],
) -> str:
    statuses = [str(getter(item["run"]) or "unavailable").casefold() for item in rows]
    return _ratio_text(statuses, success=success)


def _ratio_text(statuses: Sequence[str], *, success: set[str]) -> str:
    """Show success over all attempts, preserving non-success reasons."""
    if not statuses:
        return "未记录"
    labels = {
        "failed": "失败",
        "blocked": "阻断",
        "unavailable": "未记录",
        "unknown": "状态未提供",
        "partial": "部分记录",
        "not_applicable": "不适用",
    }
    details: list[str] = []
    for status in (
        "failed",
        "blocked",
        "unavailable",
        "unknown",
        "partial",
        "not_applicable",
    ):
        count = statuses.count(status)
        if count:
            details.append(f"{labels[status]} {count}")
    value = f"{sum(status in success for status in statuses)} / {len(statuses)}"
    return f"{value}（{'，'.join(details)}）" if details else value


def _cad_test_run_status(run: Mapping[str, Any]) -> str:
    cad_test = run.get("cad_test")
    if not isinstance(cad_test, Mapping):
        return "unavailable"
    status = str(cad_test.get("status") or "").casefold()
    if status in {"passed", "pass", "succeeded"}:
        return "passed"
    if status in {"failed", "fail", "error"}:
        return "failed"
    if status in {"not_applicable", "not applicable", "skipped"}:
        return "not_applicable"
    return "unavailable"


def _final_run_status(run: Mapping[str, Any]) -> str:
    if run.get("passed") is True:
        return "passed"
    if run.get("executed") is True:
        return "failed"
    if run.get("executed") is False:
        return "blocked"
    return "unavailable"


def _artifact_gate_status(gate: Mapping[str, Any]) -> str:
    if not gate:
        return "unavailable"
    return "passed" if gate.get("passed") is True else "failed"


def _summary_cards(report: Mapping[str, Any]) -> str:
    rows = _rows(report)
    total = len(rows)
    passed = sum(1 for item in rows if item["run"].get("passed") is True)
    cad_test_value = _status_metric(
        rows,
        _cad_test_run_status,
        measured={"passed", "failed", "not_applicable"},
        success={"passed"},
    )
    live_value = _status_metric(
        rows,
        lambda run: _process_value(run, "status"),
        measured={"measured", "partial"},
        success={"measured"},
    )
    geometry_value = _status_metric(
        rows,
        lambda run: _process_value(run, "geometry_status"),
        measured={"passed", "failed"},
        success={"passed"},
    )
    dfm_value = _status_metric(
        rows,
        lambda run: _process_value(run, "dfm_status"),
        measured={"passed", "failed"},
        success={"passed"},
    )
    average_calls = _average(rows, "total_call_count")
    average_duration = _average(rows, "total_duration_ms")
    cards = (
        ("最终通过", f"{passed} / {total}", "CADTest 与交付 gate"),
        ("CADTest 通过", cad_test_value, "需要真值的独立验收"),
        ("Live 过程记录", live_value, "真实执行过程"),
        ("几何验证通过", geometry_value, "Live 几何检查"),
        ("DFM 验证通过", dfm_value, "Live DFM 检查"),
        ("平均工具调用", _format_count(average_calls), "每个 run"),
        ("平均执行耗时", _format_duration(average_duration), "活动耗时"),
    )
    return "\n".join(
        f'<div class="metric"><div class="metric-label">{html.escape(label)}</div>'
        f'<div class="metric-value">{_metric_value_html(value)}</div>'
        f'<div class="metric-note">{html.escape(note)}</div></div>'
        for label, value, note in cards
    )


def _failure_category(run: Mapping[str, Any]) -> str:
    if run.get("passed") is True:
        return "通过"
    error = run.get("error")
    if isinstance(error, Mapping):
        error_text = f"{error.get('type', '')} {error.get('message', '')}"
    else:
        error_text = str(error or "")
    lowered = error_text.lower()
    if "planner model" in lowered or "overall dimension" in lowered or "hole_group" in lowered:
        return "规划阶段失败"
    if "geometry_validation_failed" in lowered:
        return "几何验证失败"
    if "repair_no_progress" in lowered:
        return "FreeCAD 修复无进展"
    if "acceptance_scope_invalid" in lowered:
        return "验收范围无效"
    cad_test = run.get("cad_test")
    if isinstance(cad_test, Mapping) and cad_test.get("status") in {"failed", "error"}:
        return "CADTest 验收失败"
    artifact_gate = run.get("artifact_gate")
    artifact_gate = artifact_gate if isinstance(artifact_gate, Mapping) else {}
    if artifact_gate.get("passed") is False:
        return "交付或验收未通过"
    return "未完成"


def _failure_summary(report: Mapping[str, Any]) -> str:
    categories = Counter(
        _failure_category(item["run"])
        for item in _rows(report)
        if item["run"].get("passed") is not True
    )
    if not categories:
        return '<p class="empty">本次没有失败 case。</p>'
    return "\n".join(
        f'<span class="failure-chip"><strong>{html.escape(category)}</strong>'
        f' {count} 个</span>'
        for category, count in categories.most_common()
    )


def _case_table(report: Mapping[str, Any]) -> str:
    body: list[str] = []
    for case_index, case, runs in _case_entries(report):
        run_items = [{"run": run} for run in runs]
        average_calls = _average(run_items, "total_call_count")
        average_duration = _average(run_items, "total_duration_ms")
        final_result = _run_ratio(
            runs,
            _final_run_status,
            measured={"passed", "failed", "blocked"},
            success={"passed"},
        )
        cad_result = _run_ratio(
            runs,
            _run_cad_test_status,
            measured={"passed", "failed", "not_applicable"},
            success={"passed"},
        )
        artifact_result = _run_ratio(
            runs,
            _run_artifact_status,
            measured={"passed", "failed"},
            success={"passed"},
        )
        process_result = _run_ratio(
            runs,
            _run_process_status,
            measured={"measured", "partial"},
            success={"measured"},
        )
        geometry_result = _run_ratio(
            runs,
            lambda run: _run_quality_status(run, "geometry_status"),
            measured={"passed", "failed"},
            success={"passed"},
        )
        dfm_result = _run_ratio(
            runs,
            lambda run: _run_quality_status(run, "dfm_status"),
            measured={"passed", "failed"},
            success={"passed"},
        )
        body.append(
            "<tr>"
            f"<td><code>{_escape(case.get('id'))}</code></td>"
            f"<td>{_escape(case.get('difficulty'))}</td>"
            f"<td>{_escape(len(runs))}</td>"
            f"<td>{_escape(final_result)}</td>"
            f"<td>{_escape(cad_result)}</td>"
            f"<td>{_escape(artifact_result)}</td>"
            f"<td>{_escape(process_result)}</td>"
            f"<td>{_escape(geometry_result)}</td>"
            f"<td>{_escape(dfm_result)}</td>"
            f"<td>{_format_count(average_calls)} / {_format_duration(average_duration)}</td>"
            f"<td><a href="#case-{case_index}">查看</a></td>"
            "</tr>"
        )
    return "\n".join(body) or '<tr><td colspan="11">没有 case 记录。</td></tr>'


def _key_value_rows(values: Mapping[str, Any], *, duration: bool = False) -> str:
    rows: list[str] = []
    for key, value in values.items():
        display = _format_duration(value) if duration else _escape(value)
        rows.append(f"<tr><th>{_escape(key)}</th><td>{display}</td></tr>")
    return "\n".join(rows) or '<tr><td colspan="2">没有记录。</td></tr>'


def _tool_calls(snapshot: Mapping[str, Any]) -> str:
    rows: list[str] = []
    for call in snapshot.get("tool_calls", ()) or ():
        if not isinstance(call, Mapping):
            continue
        rows.append(
            "<tr>"
            f"<td>{_escape(call.get('sequence'))}</td>"
            f"<td><code>{_escape(call.get('name'))}</code></td>"
            f"<td>{_status_cell(call.get('status'))}</td>"
            f"<td>{_format_duration(call.get('duration_ms'))}</td>"
            f"<td>{_escape(call.get('call_kind'))}</td>"
            "</tr>"
        )
    return "\n".join(rows) or '<tr><td colspan="5">没有工具调用记录。</td></tr>'


def _timeline(snapshot: Mapping[str, Any]) -> str:
    rows: list[str] = []
    for event in snapshot.get("timeline", ()) or ():
        if not isinstance(event, Mapping):
            continue
        payload = event.get("payload")
        payload = payload if isinstance(payload, Mapping) else {}
        rows.append(
            "<tr>"
            f"<td>{_escape(event.get('sequence'))}</td>"
            f"<td><code>{_escape(event.get('event_type'))}</code></td>"
            f"<td>{_status_cell(event.get('status'))}</td>"
            f"<td>{_format_duration(payload.get('duration_ms', event.get('duration_ms')))}</td>"
            "</tr>"
        )
    return "\n".join(rows) or '<tr><td colspan="4">没有时间线记录。</td></tr>'


def _attempt_panel(
    case: Mapping[str, Any],
    run: Mapping[str, Any],
    *,
    case_index: int,
    run_index: int,
) -> str:
        snapshot = run.get("live_evaluation")
        snapshot = snapshot if isinstance(snapshot, Mapping) else {}
        process = run.get("process_evaluation")
        process = process if isinstance(process, Mapping) else {}
        duration = snapshot.get("resource_cost", {})
        duration = duration.get("duration", {}) if isinstance(duration, Mapping) else {}
        duration = duration if isinstance(duration, Mapping) else {}
        stage_durations = duration.get("by_stage_ms", {})
        stage_durations = stage_durations if isinstance(stage_durations, Mapping) else {}
        live_path = run.get("live_report_path")
        error = run.get("error")
        if isinstance(error, Mapping):
            error = error.get("message") or json.dumps(error, ensure_ascii=False)
        error_block = (
            f'<div class="error"><strong>失败信息</strong><pre>{_escape(error)}</pre></div>'
            if error
            else ""
        )
        evidence = snapshot.get("evidence")
        evidence = evidence if isinstance(evidence, list) else []
        panel_id = f"attempt-{case_index}-{run_index}"
        return (
            f'<div class="attempt-panel" id="{panel_id}" data-attempt-panel="{case_index}"'
            f' data-run-index="{run_index}" hidden>'
            f'<div class="detail-grid"><div><span>最终状态</span>{_status_cell(_final_run_status(run))}</div>'
            f'<div><span>Live</span>{_status_cell(snapshot.get("status"))}</div>'
            f'<div><span>几何</span>{_status_cell(process.get("geometry_status"))}</div>'
            f'<div><span>DFM</span>{_status_cell(process.get("dfm_status"))}</div></div>'
            f'{error_block}'
            '<details><summary>CADTest 与交付结果</summary>'
            f'<pre>{_escape(json.dumps(run.get("cad_test") or {}, ensure_ascii=False, indent=2))}</pre>'
            f'<pre>{_escape(json.dumps(run.get("artifact_gate") or {}, ensure_ascii=False, indent=2))}</pre>'
            '</details>'
            '<details><summary>阶段耗时</summary>'
            f'<table class="compact"><tbody>{_key_value_rows(stage_durations, duration=True)}</tbody></table>'
            '</details>'
            '<details><summary>工具调用</summary>'
            '<table class="compact"><thead><tr><th>序号</th><th>名称</th><th>状态</th><th>耗时</th><th>类型</th></tr></thead>'
            f'<tbody>{_tool_calls(snapshot)}</tbody></table></details>'
            '<details><summary>时间线</summary>'
            '<table class="compact"><thead><tr><th>序号</th><th>事件</th><th>状态</th><th>耗时</th></tr></thead>'
            f'<tbody>{_timeline(snapshot)}</tbody></table></details>'
            f'<p class="source">Live 原始报告：<code>{_escape(live_path)}</code> · 证据条数：{len(evidence)}</p>'
            '</div>'
        )


def _case_details(report: Mapping[str, Any]) -> str:
    sections: list[str] = []
    for case_index, case, runs in _case_entries(report):
        options = []
        panels = []
        for run_index, run in enumerate(runs):
            repeat = run.get("repeat", run_index + 1)
            status = _status(_final_run_status(run))
            selected = " selected" if run_index == 0 else ""
            options.append(
                f'<option value="attempt-{case_index}-{run_index}"{selected}>'
                f'第 {html.escape(str(repeat))} 次 · {html.escape(status)}</option>'
            )
            panel = _attempt_panel(
                case,
                run,
                case_index=case_index,
                run_index=run_index,
            )
            if run_index == 0:
                panel = panel.replace(" hidden>", ">", 1)
            panels.append(panel)
        sections.append(
            f'<article class="case-card" id="case-{case_index}">'
            f'<div class="case-heading"><div><h3><code>{_escape(case.get("id"))}</code></h3>'
            f'<p class="prompt">{_escape(case.get("description"))}</p></div>'
            f'<label class="attempt-picker">查看尝试<select data-attempt-select="{case_index}">'
            f'{"".join(options)}</select></label></div>'
            f'<p class="attempt-count">共 {len(runs)} 次尝试；选择某次尝试查看完整过程。</p>'
            f'{"".join(panels)}'
            '</article>'
        )
    return "\n".join(sections) or '<p class="empty">没有 case 详情。</p>'


_STYLES = """
:root { color-scheme: light; --ink: #172033; --muted: #64748b; --line: #dbe3ee; --panel: #ffffff; --bg: #f4f7fb; --accent: #2563eb; --ok: #15803d; --warn: #b45309; --bad: #b91c1c; }
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--ink); font: 14px/1.55 system-ui, -apple-system, "Segoe UI", sans-serif; }
.container { width: min(1480px, calc(100% - 40px)); margin: 0 auto; }
header.hero { padding: 42px 0 28px; border-bottom: 1px solid var(--line); background: #fff; }
.eyebrow { margin: 0 0 8px; color: var(--accent); font-size: 12px; font-weight: 700; letter-spacing: .08em; text-transform: uppercase; }
h1 { margin: 0; font-size: clamp(26px, 4vw, 42px); letter-spacing: 0; }
h2 { margin: 34px 0 14px; font-size: 22px; }
h3 { margin: 0 0 10px; font-size: 18px; }
.subline, .source, .metric-note, .prompt, .empty { color: var(--muted); }
.meta { display: flex; flex-wrap: wrap; gap: 8px 18px; margin-top: 16px; color: var(--muted); }
main { padding: 4px 0 52px; }
.metrics { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 12px; }
.metric, .case-card { background: var(--panel); border: 1px solid var(--line); border-radius: 8px; }
.metric { padding: 16px; min-height: 116px; }
.metric-label { color: var(--muted); font-size: 13px; font-weight: 650; }
.metric-value { margin-top: 7px; font-size: 27px; font-weight: 750; line-height: 1.15; }
.metric-main { display: block; }
.metric-detail { display: block; margin-top: 6px; color: var(--muted); font-size: 12px; font-weight: 550; line-height: 1.35; }
.metric-note { margin-top: 3px; font-size: 12px; }
.failure-strip { display: flex; flex-wrap: wrap; gap: 8px; }
.failure-chip { padding: 7px 10px; border: 1px solid #f0c7c7; border-radius: 6px; background: #fff7f7; color: var(--bad); }
.table-wrap { overflow-x: auto; background: var(--panel); border: 1px solid var(--line); border-radius: 8px; }
table { width: 100%; border-collapse: collapse; min-width: 920px; }
th, td { padding: 11px 12px; border-bottom: 1px solid var(--line); text-align: left; vertical-align: top; }
th { background: #f8fafc; color: var(--muted); font-size: 12px; font-weight: 700; white-space: nowrap; }
tr:last-child td { border-bottom: 0; }
tbody tr:hover { background: #f8fbff; }
code { font-family: ui-monospace, SFMono-Regular, Consolas, monospace; font-size: .92em; overflow-wrap: anywhere; }
a { color: var(--accent); }
.status { display: inline-block; padding: 2px 7px; border-radius: 999px; font-size: 12px; font-weight: 700; white-space: nowrap; }
.status-pass { color: var(--ok); background: #eaf7ee; }
.status-fail { color: var(--bad); background: #fff0f0; }
.status-warn { color: var(--warn); background: #fff7e6; }
.status-muted { color: var(--muted); background: #eef2f7; }
.case-list { display: grid; gap: 14px; }
.case-card { padding: 18px; scroll-margin-top: 16px; }
.case-heading { display: flex; align-items: flex-start; justify-content: space-between; gap: 18px; }
.case-heading h3 { margin-bottom: 4px; }
.attempt-picker { display: flex; align-items: center; gap: 8px; color: var(--muted); font-size: 13px; white-space: nowrap; }
.attempt-picker select { min-width: 190px; padding: 7px 30px 7px 9px; border: 1px solid var(--line); border-radius: 6px; background: #fff; color: var(--ink); font: inherit; }
.attempt-count { margin: -6px 0 12px; color: var(--muted); font-size: 12px; }
.attempt-panel[hidden] { display: none; }
.attempt-panel { border-top: 1px solid var(--line); padding-top: 14px; }
.case-card .prompt { margin: 0 0 14px; white-space: pre-wrap; }
.detail-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 8px; margin-bottom: 14px; }
.detail-grid > div { padding: 10px; background: #f8fafc; border-radius: 6px; }
.detail-grid span { display: block; margin-bottom: 5px; color: var(--muted); font-size: 12px; }
details { margin-top: 10px; border-top: 1px solid var(--line); }
summary { cursor: pointer; padding: 11px 0; color: var(--accent); font-weight: 700; }
.compact { min-width: 0; font-size: 13px; }
.compact th, .compact td { padding: 7px 9px; }
pre { max-height: 420px; overflow: auto; padding: 12px; border-radius: 6px; background: #111827; color: #e5e7eb; white-space: pre-wrap; overflow-wrap: anywhere; }
.error { margin: 12px 0; padding: 12px; border-left: 3px solid var(--bad); background: #fff7f7; }
.error pre { margin-bottom: 0; background: #3a1f25; }
footer { padding: 24px 0 42px; color: var(--muted); font-size: 12px; }
@media (max-width: 680px) { .container { width: min(100% - 24px, 1480px); } header.hero { padding-top: 28px; } .metric-value { font-size: 23px; } .case-heading { display: block; } .attempt-picker { margin-top: 10px; } .attempt-picker select { width: 100%; } }
"""


_ATTEMPT_SCRIPT = """
document.querySelectorAll('[data-attempt-select]').forEach(function (select) {
  select.addEventListener('change', function () {
    var caseIndex = select.getAttribute('data-attempt-select');
    document.querySelectorAll('[data-attempt-panel="' + caseIndex + '"]').forEach(function (panel) {
      panel.hidden = panel.id !== select.value;
    });
  });
});
"""


def render_html(report: Mapping[str, Any]) -> str:
    manifest = report.get("manifest")
    manifest = manifest if isinstance(manifest, Mapping) else {}
    run_id = manifest.get("run_id") or "unknown"
    dataset_hash = manifest.get("dataset_hash") or "未记录"
    cases = manifest.get("case_ids")
    case_count = len(cases) if isinstance(cases, list) else len(_rows(report))
    repeat = manifest.get("repeat") or "未记录"
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>CADTest Batch Report - {_escape(run_id)}</title>
  <style>{_STYLES}</style>
</head>
<body>
  <header class="hero">
    <div class="container">
      <p class="eyebrow">Developer Evaluation</p>
      <h1>CADTest 批量评测报告</h1>
      <p class="subline">独立真值验收与真实执行过程记录的合并视图。页面只展示已有 JSON 报告中的事实。</p>
      <div class="meta">
        <span>Run：<code>{_escape(run_id)}</code></span>
        <span>Case：{_escape(case_count)}</span>
        <span>Repeat：{_escape(repeat)}</span>
        <span>Dataset：<code>{_escape(dataset_hash)}</code></span>
        <span>Started：<code>{_escape(manifest.get('started_at'))}</code></span>
        <span><a href="report.json">打开原始 JSON</a></span>
      </div>
    </div>
  </header>
  <main class="container">
    <section>
      <h2>总体摘要</h2>
      <div class="metrics">{_summary_cards(report)}</div>
    </section>
    <section>
      <h2>失败分类</h2>
      <div class="failure-strip">{_failure_summary(report)}</div>
    </section>
    <section>
      <h2>Case 结果</h2>
      <div class="table-wrap">
        <table>
          <thead><tr><th>Case</th><th>难度</th><th>尝试</th><th>最终通过</th><th>CADTest</th><th>交付</th><th>Live</th><th>几何</th><th>DFM</th><th>调用 / 耗时</th><th>详情</th></tr></thead>
          <tbody>{_case_table(report)}</tbody>
        </table>
      </div>
    </section>
    <section>
      <h2>Case 详情（按 Case 合并）</h2>
      <div class="case-list">{_case_details(report)}</div>
    </section>
  </main>
  <script>{_ATTEMPT_SCRIPT}</script>
  <footer><div class="container">CAD-Agent developer-only report · 原始 JSON 报告仍是机器可读的权威结果。</div></footer>
</body>
</html>
"""


def write_html_report(
    report_path: Path | str, output_path: Path | str | None = None
) -> Path:
    source = Path(report_path)
    report = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(report, Mapping):
        raise ValueError("CADTest batch report must be a JSON object")
    destination = Path(output_path) if output_path is not None else source.with_suffix(".html")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(render_html(report), encoding="utf-8")
    return destination
