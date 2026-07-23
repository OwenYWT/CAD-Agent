"""Evidence inspect report (C1) — verify build_inspect_report aggregates the
already-computed geometry facts into one honest, structured report, and that it
flows through the orchestrator onto GenerateResponse / GenerationResult.

Hermetic: real GeometryValidator on synthetic trimesh meshes, faked Docker/LLM.
"""
import asyncio

import pytest
import trimesh

from app.config import settings
from app.models.schemas import GenerationResult, InspectCheck, InspectReport
from app.validation.geometry_validator import GeometryValidator
from app.validation.inspect import add_check, build_inspect_report
from tests.e2e_harness import build_orchestrator, make_stl, patch_single_step
from app.models.schemas import CADPlan


@pytest.fixture(autouse=True)
def isolated_storage(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "file_storage_dir", str(tmp_path / "files"))
    patch_single_step(monkeypatch)


def _plan(desc="测试零件"):
    return CADPlan(
        description=desc, part_type="custom", dimensions={},
        features=[], constraints=[], ambiguities=[], modeling_hint="extrude_cut",
    )


def _check(report, name):
    return next(c for c in report.checks if c.name == name)


# ============================================================================
# Unit: build_inspect_report against the real validator
# ============================================================================

@pytest.mark.asyncio
async def test_report_passes_for_clean_cube():
    geo = await GeometryValidator().validate(make_stl("printable"))
    report = build_inspect_report(geo)

    assert report.verdict == "pass"
    assert report.printable is True
    assert report.is_watertight is True
    assert report.bounding_box is not None
    assert report.volume > 0
    # every geometry rule became a check
    assert len(report.checks) == len(geo.rules)
    assert _check(report, "watertight").status == "pass"
    assert all(c.source == "geometry" for c in report.checks)


@pytest.mark.asyncio
async def test_report_fails_for_non_watertight():
    geo = await GeometryValidator().validate(make_stl("non_watertight"))
    report = build_inspect_report(geo)

    assert report.verdict == "fail"
    assert report.printable is False
    assert _check(report, "watertight").status == "fail"
    assert any("水密" in w for w in report.print_warnings)


@pytest.mark.asyncio
async def test_report_fails_for_oversized():
    geo = await GeometryValidator().validate(make_stl("oversized"))
    report = build_inspect_report(geo)

    assert report.verdict == "fail"
    assert _check(report, "build_volume").status == "fail"
    assert report.printable is False


@pytest.mark.asyncio
async def test_thin_wall_warns_but_stays_printable():
    """A thin watertight box: min_wall is advisory → warn, never fail. Printable stays True."""
    geo = await GeometryValidator().validate(make_stl("thin"))
    report = build_inspect_report(geo)

    assert report.printable is True
    # verdict is at most "warn" (min_wall warning), never "fail"
    assert report.verdict in ("pass", "warn")
    assert _check(report, "min_wall").status in ("pass", "warn")


def test_dfm_enrichment_folds_in_score_and_violations():
    """A non-DFM report is enriched in place with score + violations from the DFM dict."""
    # Build a minimal report by hand (no geometry needed for this unit)
    from app.validation.geometry_validator import GeometryValidation, ValidationRule
    geo = GeometryValidation(
        passed=True,
        rules=[ValidationRule("watertight", True, "ok", "info")],
        bounding_box={"x_min": 0, "x_max": 1, "y_min": 0, "y_max": 1, "z_min": 0, "z_max": 1},
        volume=1.0, is_watertight=True, printable=True, fits_build_volume=True,
    )
    dfm = {
        "design_score": 75,
        "rule_violations": [{
            "rule_id": "fdm_wall_thickness", "process": "fdm", "category": "wall_thickness",
            "severity": "critical", "source": "geometric", "actual_value": 0.6,
            "message": "壁厚不足", "suggestion": "加厚",
        }],
    }
    report = build_inspect_report(geo, dfm=dfm)
    assert report.design_score == 75
    assert len(report.dfm_violations) == 1
    assert report.dfm_violations[0].rule_id == "fdm_wall_thickness"


def test_add_check_recomputes_verdict():
    report = InspectReport(verdict="pass", checks=[
        InspectCheck(name="watertight", status="pass", message="ok"),
    ])
    add_check(report, InspectCheck(name="vision", status="warn", message="未执行", source="vision"))
    assert report.verdict == "warn"
    assert _check(report, "vision").source == "vision"


# ============================================================================
# Integration: report reaches the API responses
# ============================================================================

@pytest.mark.asyncio
async def test_inspect_report_on_generate_response(tmp_path):
    # Provide a render + passing vision so the visual check passes (verdict stays "pass").
    png = tmp_path / "iso.png"
    png.write_bytes(b"\x89PNG\r\n")
    orch = build_orchestrator(
        plan=_plan(desc="手机支架"),
        executor_outcomes=[{"success": True, "stl": "printable"}],
        renderer_paths=[png],
    )
    r = await orch.generate("做一个手机支架")
    assert r.success is True
    assert r.inspect_report is not None
    assert r.inspect_report.verdict == "pass"
    assert r.inspect_report.printable is True
    # parity with the existing validation payload
    assert r.inspect_report.is_watertight == r.validation.is_watertight
    assert sorted(r.inspect_report.available_exports) == ["step", "stl"]
    assert r.inspect_report.repair_attempts == 0
    assert r.inspect_report.source == "geometry_validator"


@pytest.mark.asyncio
async def test_inspect_report_flows_through_handle_message(tmp_path):
    """The WS path (handle_message → GenerationResult) must carry the report too."""
    from app.agent.orchestrator import ConversationContext
    png = tmp_path / "iso.png"
    png.write_bytes(b"\x89PNG\r\n")
    orch = build_orchestrator(
        plan=_plan(desc="一个零件"),
        executor_outcomes=[{"success": True, "stl": "printable"}],
        renderer_paths=[png],
    )
    ctx = ConversationContext(session_id="s1")
    result = await orch.handle_message(ctx, "做一个零件")
    assert isinstance(result, GenerationResult)
    assert result.inspect_report is not None
    assert result.inspect_report.verdict == "pass"


@pytest.mark.asyncio
async def test_oversized_generate_reports_fail_but_success_true():
    """Honest contract: success=True (model produced) but inspect verdict=fail / not printable."""
    orch = build_orchestrator(
        plan=_plan(desc="超大零件"),
        executor_outcomes=[{"success": True, "stl": "oversized"}],
    )
    r = await orch.generate("一个超大零件")
    assert r.success is True               # model was produced
    assert r.inspect_report.verdict == "fail"
    assert r.inspect_report.printable is False

def test_inspect_report_serializes_delivery_evidence():
    report = InspectReport(
        verdict="warn",
        available_exports=["stl", "step"],
        repair_attempts=2,
        source="geometry_validator",
    )

    dumped = report.model_dump()

    assert dumped["available_exports"] == ["stl", "step"]
    assert dumped["repair_attempts"] == 2
    assert dumped["source"] == "geometry_validator"


@pytest.mark.asyncio
async def test_build_inspect_report_includes_exports_repairs_and_source():
    geo = await GeometryValidator().validate(make_stl("printable"))

    report = build_inspect_report(
        geo,
        available_exports=["stl", "step"],
        repair_attempts=1,
        source="geometry_validator",
    )

    assert report.available_exports == ["stl", "step"]
    assert report.repair_attempts == 1
    assert report.source == "geometry_validator"
