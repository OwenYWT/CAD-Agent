"""Regression guard for the supported cadquery-ocp casting APIs."""

from app.dfm.step_analyzer_script import STEP_ANALYSIS_SCRIPT


def test_step_analyzer_supports_old_and_new_topods_casting_names():
    assert 'getattr(TopoDS, f"{kind}_s", None)' in STEP_ANALYSIS_SCRIPT
    assert "or getattr(TopoDS, kind, None)" in STEP_ANALYSIS_SCRIPT
    assert "TopoDS.Face_s(" not in STEP_ANALYSIS_SCRIPT
    assert "TopoDS.Edge_s(" not in STEP_ANALYSIS_SCRIPT
