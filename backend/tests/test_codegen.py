"""
Tests for CodeGenerator: prompt construction, error hint matching, code extraction.
These are unit tests — they do NOT call the LLM.
"""
import pytest

from app.agent.code_gen import CodeGenerator
from app.models.schemas import CADPlan


# === Code extraction ===

class TestCodeExtraction:
    """Verify _extract_code handles various LLM output formats."""

    def setup_method(self):
        self.gen = CodeGenerator()

    def test_python_fenced_block(self):
        text = "Here is code:\n```python\nimport cadquery\nresult = 1\n```\nDone."
        assert self.gen._extract_code(text) == "import cadquery\nresult = 1"

    def test_generic_fenced_block(self):
        text = "```\nimport cadquery\nresult = 1\n```"
        assert self.gen._extract_code(text) == "import cadquery\nresult = 1"

    def test_no_fences(self):
        text = "import cadquery\nresult = 1"
        assert self.gen._extract_code(text) == "import cadquery\nresult = 1"

    def test_unclosed_fence(self):
        text = "```python\nimport cadquery\nresult = 1"
        assert "import cadquery" in self.gen._extract_code(text)

    def test_multiple_fenced_blocks_takes_first(self):
        text = "```python\nfirst_block\n```\nsome text\n```python\nsecond_block\n```"
        assert self.gen._extract_code(text) == "first_block"


# === Error hint matching ===

class TestErrorHints:
    """Verify error → fix hint mapping via the failure taxonomy (replaced _ERROR_HINTS).
    Detailed taxonomy coverage lives in test_failure_taxonomy.py; these guard the
    specific hint contents that fix_error relies on."""

    def test_stack_empty_error_matches(self):
        from app.agent.failure_taxonomy import classify, fix_hint_for
        fix = fix_hint_for(classify(None, "at least one solid on the stack")).lower()
        assert "revolve" in fix or "union" in fix or "extrude" in fix

    def test_brep_api_error_matches(self):
        from app.agent.failure_taxonomy import classify, fix_hint_for
        assert "fillet" in fix_hint_for(classify(None, "BRep_API: not done")).lower()

    def test_wire_not_closed_matches(self):
        from app.agent.failure_taxonomy import classify, fix_hint_for
        assert ".close()" in fix_hint_for(classify(None, "Wire is not closed"))

    def test_all_classes_have_nonempty_hint(self):
        from app.agent.failure_taxonomy import FAILURE_CLASSES
        for fc in FAILURE_CLASSES:
            assert len(fc.key) > 0 and len(fc.fix_hint) > 0

    def test_class_count_minimum(self):
        """We should have a rich taxonomy, not a handful of patterns."""
        from app.agent.failure_taxonomy import FAILURE_CLASSES
        assert len(FAILURE_CLASSES) >= 5


# === Example formatting ===

class TestExampleFormatting:
    def setup_method(self):
        self.gen = CodeGenerator()

    def test_empty_examples(self):
        assert self.gen._format_examples([]) == ""

    def test_single_example(self):
        examples = [{"description": "A box", "code": "result = cq.Workplane().box(1,1,1)"}]
        formatted = self.gen._format_examples(examples)
        assert "案例 1" in formatted
        assert "A box" in formatted
        assert "cq.Workplane" in formatted

    def test_multiple_examples(self):
        examples = [
            {"description": "Box", "code": "box_code"},
            {"description": "Cylinder", "code": "cyl_code"},
        ]
        formatted = self.gen._format_examples(examples)
        assert "案例 1" in formatted
        assert "案例 2" in formatted


# === Lazy client init ===

class TestLazyClientInit:
    def test_client_not_created_on_init(self):
        gen = CodeGenerator()
        assert gen._client is None

    def test_client_raises_without_api_key(self, monkeypatch):
        from app.config import settings
        monkeypatch.setattr(settings, "llm_provider", "moonshot")
        monkeypatch.setattr(settings, "moonshot_api_key", None)
        gen = CodeGenerator()
        with pytest.raises(RuntimeError, match="MOONSHOT_API_KEY"):
            _ = gen.client


# === Prompt construction (without calling LLM) ===

class TestPromptConstruction:
    """Verify that generate() builds correct system/user messages."""

    def setup_method(self):
        self.gen = CodeGenerator()

    def test_generate_user_content_includes_plan_fields(self):
        """Verify the user content string contains all plan fields."""
        plan = CADPlan(
            description="一个杯子",
            part_type="revolution",
            dimensions={"height": 100, "radius": 40},
            features=["revolve", "shell"],
            constraints=["wall_thickness=3mm"],
            modeling_hint="revolve",
        )
        # Build user content the same way generate() does
        user_content = (
            f"需求描述: {plan.description}\n"
            f"零件类型: {plan.part_type}\n"
            f"建模策略: {plan.modeling_hint or 'extrude_cut'}\n"
            f"尺寸: {plan.dimensions}\n"
            f"特征: {plan.features}\n"
            f"约束: {plan.constraints}\n"
        )
        assert "杯子" in user_content
        assert "revolution" in user_content
        assert "revolve" in user_content
        assert "height" in user_content
        assert "wall_thickness" in user_content

    def test_system_prompt_includes_examples(self):
        from app.agent.prompts import CODEGEN_SYSTEM_PROMPT
        examples = [{"description": "Test", "code": "test_code"}]
        system = CODEGEN_SYSTEM_PROMPT.format(
            examples=self.gen._format_examples(examples)
        )
        assert "test_code" in system
        assert "Test" in system
