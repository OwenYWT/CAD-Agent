"""Static analysis of generated CadQuery source.

`analyze_code` runs on every generation, on text a model just produced, so it
has to be both correct and bounded. The loop check previously used a regex with
nested quantifiers over overlapping classes; on a loop body of ~14 lines with no
later `show_object` it took 17 seconds, growing roughly 16x per two additional
lines, and it ran on the event loop. These tests pin both properties.
"""
from __future__ import annotations

import time

import pytest

from app.sandbox.code_analyzer import CadQueryAnalyzer, analyze_code

_inside_loop = CadQueryAnalyzer._show_object_inside_loop


@pytest.mark.parametrize(
    "code, expected",
    [
        ("for i in range(4):\n    show_object(part)\n", True),
        ("while remaining:\n    if remaining:\n        show_object(part)\n", True),
        ("for i in range(4):\n    result = result.union(p)\nshow_object(result)\n", False),
        ("for i in a:\n    pass\nfor j in b:\n    pass\nshow_object(result)\n", False),
        # A commented-out call is not a call.
        ("for i in a:\n    # show_object(part)\n    pass\nshow_object(result)\n", False),
        ("show_object(result)\n", False),
        ("", False),
        # Dedent closes the loop, so this one is at module level.
        ("for i in a:\n    x = 1\nshow_object(result)\n", False),
    ],
)
def test_show_object_inside_loop_detection(code, expected):
    assert _inside_loop(code) is expected


def test_loop_scan_is_linear_not_exponential():
    """The exact shape that used to hang the worker: a loop, no show_object."""
    code = "for i in range(4):\n" + "".join(
        f"    value = {i} + 1\n" for i in range(4000)
    )
    started = time.time()
    assert _inside_loop(code) is False
    assert time.time() - started < 1.0


def test_analyze_code_terminates_on_a_long_loop_body():
    code = (
        "import cadquery as cq\n"
        "result = cq.Workplane('XY').box(10, 10, 10)\n"
        "for i in range(6):\n"
        + "".join(f"    offset_{i} = {i} * 2.0\n" for i in range(60))
        + "show_object(result)\n"
    )
    started = time.time()
    warnings = analyze_code(code)
    assert time.time() - started < 1.0
    assert not any("循环体内" in item for item in warnings)


def test_multiple_show_object_calls_are_still_reported():
    warnings = analyze_code(
        "result = 1\nshow_object(a)\nshow_object(b)\n"
    )
    assert any("show_object" in item for item in warnings)
