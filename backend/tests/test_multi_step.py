"""
Tests for multi-step build planning module.
Unit tests — no LLM calls.
"""
import pytest

from app.agent.multi_step import BuildStep, BuildPlan, BuildPhase


class TestBuildStep:
    def test_defaults(self):
        step = BuildStep(phase=BuildPhase.BASE, description="Create base box")
        assert step.phase == BuildPhase.BASE
        assert step.code_snippet == ""
        assert step.success is False
        assert step.error is None

    def test_with_code(self):
        step = BuildStep(
            phase=BuildPhase.PRIMARY,
            description="Add holes",
            code_snippet="result = result.faces('>Z').hole(5)",
            success=True,
        )
        assert step.success
        assert "hole" in step.code_snippet


class TestBuildPlan:
    def test_creation(self):
        steps = [
            BuildStep(phase=BuildPhase.BASE, description="Base"),
            BuildStep(phase=BuildPhase.PRIMARY, description="Features"),
            BuildStep(phase=BuildPhase.REFINEMENT, description="Fillets"),
        ]
        plan = BuildPlan(steps=steps, complexity="moderate")
        assert len(plan.steps) == 3
        assert plan.complexity == "moderate"

    def test_phases_order(self):
        """BuildPhase enum values exist for standard CAD workflow."""
        assert BuildPhase.BASE == "base"
        assert BuildPhase.PRIMARY == "primary"
        assert BuildPhase.SECONDARY == "secondary"
        assert BuildPhase.REFINEMENT == "refinement"


class TestStandardPartsContext:
    """Verify that standard parts lookup works and can produce prompt context."""

    def test_parts_lookup_returns_data(self):
        from app.parts_library.data import lookup
        result = lookup("M5")
        assert result is not None, "Should find M5 screws"
        assert "pitch" in result

    def test_parts_format_for_prompt(self):
        from app.parts_library.data import format_for_prompt
        prompt_text = format_for_prompt("M5")
        assert "M5" in prompt_text
        assert len(prompt_text) > 10

    def test_bearing_lookup(self):
        from app.parts_library.data import lookup
        result = lookup("608")
        assert result is not None
        assert "inner" in result

    def test_unknown_part_returns_none(self):
        from app.parts_library.data import lookup
        assert lookup("XXXXX") is None


class TestPlanDecomposerInit:
    """Verify PlanDecomposer can be imported and uses lazy client."""

    def test_import(self):
        from app.agent.multi_step import PlanDecomposer
        decomposer = PlanDecomposer()
        assert decomposer._client is None

    def test_lazy_client_raises_without_key(self, monkeypatch):
        from app.config import settings
        monkeypatch.setattr(settings, "llm_provider", "moonshot")
        monkeypatch.setattr(settings, "moonshot_api_key", None)
        from app.agent.multi_step import PlanDecomposer
        decomposer = PlanDecomposer()
        with pytest.raises(RuntimeError, match="MOONSHOT_API_KEY"):
            _ = decomposer.client

    @pytest.mark.asyncio
    async def test_simple_parameterized_plan_never_calls_provider(self):
        from app.agent.multi_step import PlanDecomposer
        from app.models.schemas import CADPlan

        decomposer = PlanDecomposer()
        plan = CADPlan(
            description="Shelf bracket",
            part_type="bracket",
            dimensions={"width": 40, "height": 60},
            features=["two holes"],
        )

        build_plan = await decomposer.decompose(plan)

        assert build_plan.complexity == "simple"
        assert len(build_plan.steps) == 1
        assert decomposer._client is None
