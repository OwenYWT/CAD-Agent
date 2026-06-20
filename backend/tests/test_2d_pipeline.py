"""
Tests for the 2D (ezdxf/DXF) pipeline.
Unit tests that verify code filter, prompt selection, and mode routing.
"""
import pytest

from app.sandbox.code_filter import validate_code


class TestDxfCodeValidation:
    """Verify that ezdxf-only code (no cadquery) passes validation."""

    def test_pure_ezdxf_code(self):
        code = """\
import ezdxf
doc = ezdxf.new()
msp = doc.modelspace()
msp.add_circle((0, 0), radius=25)
doc.saveas("/tmp/output/result.dxf")
"""
        ok, msg = validate_code(code)
        assert ok, f"Pure ezdxf code should pass validation: {msg}"

    def test_ezdxf_with_math(self):
        code = """\
import ezdxf
import math
doc = ezdxf.new()
msp = doc.modelspace()
r = 25
for i in range(6):
    angle = math.radians(60 * i)
    msp.add_line((0, 0), (r * math.cos(angle), r * math.sin(angle)))
doc.saveas("/tmp/output/result.dxf")
"""
        ok, msg = validate_code(code)
        assert ok, f"ezdxf+math code should pass: {msg}"

    def test_ezdxf_rejects_os_import(self):
        code = """\
import ezdxf
import os
doc = ezdxf.new()
"""
        ok, msg = validate_code(code)
        assert not ok

    def test_ezdxf_with_numpy(self):
        code = """\
import ezdxf
import numpy as np
doc = ezdxf.new()
msp = doc.modelspace()
pts = np.array([(0,0), (10,0), (10,10), (0,10)])
msp.add_lwpolyline(pts.tolist(), close=True)
doc.saveas("/tmp/output/result.dxf")
"""
        ok, msg = validate_code(code)
        assert ok


class TestDxfPromptSelection:
    """Verify 2D prompt is used for 2D plans."""

    def test_ezdxf_prompt_exists(self):
        from app.agent.prompts import EZDXF_CODEGEN_PROMPT
        assert "ezdxf" in EZDXF_CODEGEN_PROMPT.lower() or "dxf" in EZDXF_CODEGEN_PROMPT.lower()

    def test_ezdxf_prompt_has_examples_placeholder(self):
        from app.agent.prompts import EZDXF_CODEGEN_PROMPT
        assert "{examples}" in EZDXF_CODEGEN_PROMPT


class TestExecutorModeRouting:
    """Verify executor passes mode correctly."""

    def test_executor_accepts_2d_mode(self):
        from app.sandbox.executor import CadQueryExecutor
        executor = CadQueryExecutor()
        # Just verify the method signature accepts mode="2d"
        import inspect
        sig = inspect.signature(executor.execute)
        assert "mode" in sig.parameters
        assert sig.parameters["mode"].default == "3d"
