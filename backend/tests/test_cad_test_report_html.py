from __future__ import annotations

import copy
import json


def _report() -> dict:
    return {
        "schema_version": "cad-agent-cadtest-batch-report.v1",
        "manifest": {
            "run_id": "20261123T064529Z_test",
            "dataset_hash": "dataset-hash",
            "case_ids": ["ctb-00000007"],
            "repeat": 1,
            "started_at": "2026-11-23T06:45:29Z",
        },
        "summary": {
            "overall": {
                "n_cases": 1,
                "pass@1": {"mean": 1.0, "std": 0.0},
                "process_evaluation": {
                    "measured_rate": 1.0,
                    "avg_total_call_count": 2.0,
                    "avg_activity_duration_ms": 125.0,
                    "geometry_pass_rate": 1.0,
                    "dfm_pass_rate": 1.0,
                },
            }
        },
        "cases": [
            {
                "id": "ctb-00000007",
                "difficulty": "simple",
                "path": "extrude_cut",
                "description": "create a <cube>",
                "runs": [
                    {
                        "case_id": "ctb-00000007",
                        "repeat": 1,
                        "passed": True,
                        "live_report_status": "finished",
                        "live_report_path": "reports/live/live_test.json",
                        "artifact_gate": {
                            "passed": True,
                            "step_readable": True,
                            "stl_readable": True,
                            "geometry_nonempty": True,
                            "visual_rendered": True,
                            "cad_test_status": "passed",
                            "native_delivery_verified": True,
                        },
                        "cad_test": {
                            "status": "passed",
                            "requirements": {"passed": 2, "total": 2},
                            "reason": "cad_test_verified",
                        },
                        "process_evaluation": {
                            "status": "measured",
                            "total_call_count": 2,
                            "total_duration_ms": 125,
                            "geometry_status": "passed",
                            "dfm_status": "passed",
                            "visual_status": "unknown",
                            "lifecycle_status": "succeeded",
                        },
                        "live_evaluation": {
                            "status": "finished",
                            "metrics": {"total_call_count": 2},
                            "tool_calls": [
                                {
                                    "sequence": 1,
                                    "name": "agent_v2.plan",
                                    "status": "passed",
                                    "duration_ms": 125,
                                }
                            ],
                            "timeline": [
                                {
                                    "sequence": 1,
                                    "event_type": "plan_created",
                                    "payload": {"duration_ms": 125},
                                }
                            ],
                            "resource_cost": {
                                "duration": {
                                    "by_stage_ms": {"planning": 125}
                                }
                            },
                        },
                    }
                ],
            }
        ],
    }


def test_render_html_contains_summary_cases_and_live_details():
    from benchmark.cad_test_report import render_html

    html = render_html(_report())

    assert "CADTest Batch Report" in html
    assert "ctb-00000007" in html
    assert "最终通过" in html
    assert "agent_v2.plan" in html
    assert "planning" in html
    assert "cad_test_verified" in html
    assert "create a &lt;cube&gt;" in html
    assert "reports/live/live_test.json" in html


def test_render_html_uses_utf8_status_labels_without_mojibake():
    from benchmark.cad_test_report import render_html

    rendered = render_html(_report())

    assert "最终通过" in rendered
    assert "通过" in rendered
    assert "閫氳繃" not in rendered
    assert "鏈褰" not in rendered


def test_summary_shows_case_success_coverage_for_repeated_runs():
    from benchmark.cad_test_report import render_html

    report = _report()
    successful_case = report["cases"][0]
    successful_case["agg"] = {"pass@k": True}
    report["manifest"]["repeat"] = 2
    successful_case["runs"].append(copy.deepcopy(successful_case["runs"][0]))

    for case_id in ("ctb-failed-1", "ctb-failed-2"):
        failed_case = copy.deepcopy(successful_case)
        failed_case["id"] = case_id
        failed_case["agg"] = {"pass@k": False}
        for run in failed_case["runs"]:
            run["case_id"] = case_id
            run["passed"] = False
            run["cad_test"]["status"] = "failed"
            run["artifact_gate"]["cad_test_status"] = "failed"
            run["artifact_gate"]["native_delivery_verified"] = False
        report["cases"].append(failed_case)

    report["summary"]["overall"]["n_cases"] = 3
    report["summary"]["overall"]["pass@k"] = 1 / 3

    rendered = render_html(report)

    assert "Case 成功覆盖率" in rendered
    assert '<span class="metric-main">33.3%</span>' in rendered
    assert '<span class="metric-detail">（1 / 3 个 Case）</span>' in rendered
    assert "至少一次运行通过" in rendered


def test_render_html_groups_repeated_runs_under_case_selector():
    from benchmark.cad_test_report import render_html

    report = _report()
    second_run = dict(report["cases"][0]["runs"][0])
    second_run["repeat"] = 2
    second_run["passed"] = False
    report["cases"][0]["runs"].append(second_run)
    report["manifest"]["repeat"] = 2

    rendered = render_html(report)

    assert rendered.count('id="case-0"') == 1
    assert 'data-attempt-select="0"' in rendered
    assert 'value="attempt-0-0" selected' in rendered
    assert 'value="attempt-0-1"' in rendered
    assert rendered.count('data-attempt-panel="0"') == 2
    assert "document.querySelectorAll('[data-attempt-select]')" in rendered


def test_write_html_report_writes_utf8_file_next_to_json(tmp_path):
    from benchmark.cad_test_report import write_html_report

    report_path = tmp_path / "report.json"
    report_path.write_text(json.dumps(_report()), encoding="utf-8")

    output = write_html_report(report_path)

    assert output == tmp_path / "report.html"
    content = output.read_text(encoding="utf-8")
    assert content.startswith("<!doctype html>")
    assert '<meta charset="utf-8">' in content


def test_summary_uses_total_attempts_for_missing_cadtest(tmp_path):
    from benchmark.cad_test_report import _case_table, render_html

    report = _report()
    run = report["cases"][0]["runs"][0]
    run["cad_test"] = {}
    run["process_evaluation"] = {
        "status": "measured",
        "geometry_status": "passed",
        "dfm_status": "failed",
    }

    rendered = render_html(report)

    assert "CADTest 通过" in rendered
    assert "未记录" in rendered
    assert '<span class="metric-main">0 / 1</span>' in rendered
    assert '<span class="metric-detail">（未记录 1）</span>' in rendered


def test_case_table_does_not_render_missing_artifact_gate_as_failure():
    from benchmark.cad_test_report import _case_table

    report = _report()
    report["cases"][0]["runs"][0]["artifact_gate"] = {}

    table = _case_table(report)

    assert "未记录" in table


def test_case_table_uses_total_attempts_as_denominator():
    from benchmark.cad_test_report import _case_table

    report = _report()
    second_run = dict(report["cases"][0]["runs"][0])
    second_run["repeat"] = 2
    second_run["cad_test"] = {}
    second_run["artifact_gate"] = {}
    report["cases"][0]["runs"].append(second_run)

    table = _case_table(report)

    assert "1 / 2（未记录 1）" in table


def test_historical_report_recomputes_strict_success_when_cadtest_is_missing():
    from benchmark.cad_test_report import render_html

    report = _report()
    run = report["cases"][0]["runs"][0]
    run["passed"] = True
    run["cad_test"] = {}
    run["artifact_gate"].pop("cad_test_status")
    run["artifact_gate"].pop("native_delivery_verified")

    rendered = render_html(report)

    assert '<div class="metric-label">最终通过</div><div class="metric-value">0 / 1</div>' in rendered
    assert '<span class="metric-main">0.0%</span>' in rendered
    assert '<span class="metric-detail">（0 / 1 个 Case）</span>' in rendered


def test_case_table_contains_valid_detail_link():
    from benchmark.cad_test_report import _case_table

    table = _case_table(_report())

    assert '<td><a href="#case-0">查看</a></td>' in table


def test_summary_shows_stable_success_and_cadtest_observation_coverage():
    from benchmark.cad_test_report import render_html

    rendered = render_html(_report())

    assert "稳定成功率" in rendered
    assert "所有重复运行均通过" in rendered
    assert "CADTest 记录覆盖率" in rendered
    assert '<span class="metric-main">100.0%</span>' in rendered
    assert '<span class="metric-detail">（1 / 1 个可交付运行）</span>' in rendered


def test_render_html_shows_dynamic_difficulty_breakdown():
    from benchmark.cad_test_report import render_html

    report = _report()
    basic = report["cases"][0]
    basic["difficulty"] = "basic"
    for difficulty in ("combined", "complex"):
        case = copy.deepcopy(basic)
        case["id"] = f"ctb-{difficulty}"
        case["difficulty"] = difficulty
        report["cases"].append(case)
    report["summary"]["overall"]["n_cases"] = 3

    rendered = render_html(report)

    assert "难度分层" in rendered
    assert "basic" in rendered
    assert "combined" in rendered
    assert "complex" in rendered


def test_failure_summary_uses_first_failed_tool_call():
    from benchmark.cad_test_report import render_html

    report = _report()
    run = report["cases"][0]["runs"][0]
    run["passed"] = False
    run["cad_test"] = {}
    run["artifact_gate"] = {}
    run["live_evaluation"]["tool_calls"] = [
        {
            "sequence": 1,
            "name": "agent_v2.requirements",
            "status": "failed",
            "duration_ms": 25,
        }
    ]

    rendered = render_html(report)

    assert "需求解析失败" in rendered


def test_batch_report_writer_emits_json_and_html(tmp_path):
    from benchmark.cad_test_batch import _write_batch_report

    report = {"manifest": {"run_id": "test"}, "summary": {}, "cases": []}

    report_path, html_path = _write_batch_report(tmp_path, report)

    assert report_path == tmp_path / "report.json"
    assert html_path == tmp_path / "report.html"
    assert json.loads(report_path.read_text(encoding="utf-8")) == report
    assert html_path.is_file()
