from __future__ import annotations

import os
import shutil
import sys
from types import SimpleNamespace

import pytest

from app.agent.code_gen import CodeGenerator
from app.sandbox.executor import CadQueryExecutor


class _ProviderMustNotRun:
    async def create(self, **_kwargs):
        raise AssertionError("assembly composition must not ask an LLM to rewrite source")


class _Assembly:
    def __init__(self) -> None:
        self.parts: list[dict] = []

    def add(self, value, *, name, loc, color) -> None:
        self.parts.append(
            {"value": value, "name": name, "position": loc, "color": color}
        )


class _Bounds:
    def __init__(
        self,
        xmin: float,
        xmax: float,
        ymin: float,
        ymax: float,
        zmin: float,
        zmax: float,
    ) -> None:
        self.xmin = xmin
        self.xmax = xmax
        self.ymin = ymin
        self.ymax = ymax
        self.zmin = zmin
        self.zmax = zmax


class _Component:
    def __init__(self, height: float, zmin: float) -> None:
        self.height = height
        self.bounds = _Bounds(-15, 15, -10, 10, zmin, zmin + height)

    def val(self):
        return self

    def BoundingBox(self):
        return self.bounds

    def translate(self, offset):
        x, y, z = offset
        translated = _Component(self.height, self.bounds.zmin + z)
        translated.bounds = _Bounds(
            self.bounds.xmin + x,
            self.bounds.xmax + x,
            self.bounds.ymin + y,
            self.bounds.ymax + y,
            self.bounds.zmin + z,
            self.bounds.zmax + z,
        )
        return translated


@pytest.mark.asyncio
async def test_assembly_combiner_isolates_equal_parameter_names(monkeypatch):
    fake_cq = SimpleNamespace(
        Assembly=_Assembly,
        Location=lambda value: tuple(value),
        Color=lambda value: value,
    )
    monkeypatch.setitem(sys.modules, "cadquery", fake_cq)
    generator = CodeGenerator()
    generator._client = SimpleNamespace(
        chat=SimpleNamespace(completions=_ProviderMustNotRun())
    )
    parts = [
        {
            "name": "part_01",
            "label": "底座",
            "code": (
                "import cadquery as cq\n"
                "height = 5.0\n"
                "def make_part_01():\n"
                "    return _Component(height, -height / 2)\n"
                "result = make_part_01()\n"
                "show_object(result)\n"
            ),
            "position": [0, 0, 0],
            "color": "steelblue",
        },
        {
            "name": "part_02",
            "label": "上盖",
            "code": (
                "import cadquery as cq\n"
                "height = 3.0\n"
                "def make_part_02():\n"
                "    return _Component(height, 0)\n"
                "result = make_part_02()\n"
                "show_object(result)\n"
            ),
            "position": [0, 0, 5],
            "color": "orange",
        },
    ]

    source = await generator.generate_assembly_combiner(parts)
    namespace = {
        "_Component": _Component,
        "show_object": lambda value: namespace.update(output=value),
    }
    exec(compile(source, "<assembly>", "exec"), namespace)

    result = namespace["output"]
    assert [part["value"].height for part in result.parts] == [5.0, 3.0]
    # ``part_position`` is the target for the centre of the component's
    # bottom bounding-box face.  Normalising that anchor makes independently
    # generated parts deterministic even when one source creates a centred
    # solid and another starts at z=0.
    assert [
        (
            (part["value"].bounds.xmin + part["value"].bounds.xmax) / 2,
            (part["value"].bounds.ymin + part["value"].bounds.ymax) / 2,
            part["value"].bounds.zmin,
        )
        for part in result.parts
    ] == [(0.0, 0.0, 0.0), (0.0, 0.0, 0.0)]
    assert [part["position"] for part in result.parts] == [
        (0.0, 0.0, 0.0),
        (0.0, 0.0, 5.0),
    ]
    global_z_bounds = [
        (
            part["value"].bounds.zmin + part["position"][2],
            part["value"].bounds.zmax + part["position"][2],
        )
        for part in result.parts
    ]
    assert global_z_bounds == [(0.0, 5.0), (5.0, 8.0)]
    assert source.count("def _assembly_component_") == 2
    assert source.count("show_object(result)") == 1


@pytest.mark.asyncio
async def test_assembly_combiner_translates_silver_to_supported_rgb(monkeypatch):
    def color(*values):
        if values == ("silver",):
            raise ValueError("Unknown color name: silver")
        return values

    monkeypatch.setitem(sys.modules, "cadquery", SimpleNamespace(
        Assembly=_Assembly, Location=tuple, Color=color,
    ))
    source = await CodeGenerator().generate_assembly_combiner([{
        "name": "wall", "code": "result = _Component(23, 0)",
        "position": [29, 0, 2], "color": "silver",
    }])
    namespace = {"_Component": _Component, "show_object": lambda value: None}
    exec(compile(source, "<silver-assembly>", "exec"), namespace)
    part = namespace["result"].parts[0]
    assert part["color"] == pytest.approx((192 / 255, 192 / 255, 192 / 255))
    assert part["position"] == (29, 0, 2)
    assert part["value"].height == 23


@pytest.mark.asyncio
@pytest.mark.skipif(
    os.getenv("RUN_REAL_PODMAN") != "1",
    reason="set RUN_REAL_PODMAN=1 to exercise the actual assembly STL exporter",
)
@pytest.mark.parametrize("lid_color", ["steelblue", "silver"])
async def test_touching_assembly_exports_a_watertight_preview_stl(lid_color) -> None:
    generator = CodeGenerator()
    source = await generator.generate_assembly_combiner([
        {
            "name": "base",
            "code": (
                "import cadquery as cq\n"
                "result = cq.Workplane('XY').box("
                "30, 20, 5, centered=(True, True, False))\n"
            ),
            "position": [0, 0, 0],
            "color": "lightgray",
        },
        {
            "name": "lid",
            "code": (
                "import cadquery as cq\n"
                "result = cq.Workplane('XY').box(30, 20, 3)\n"
            ),
            "position": [0, 0, 5],
            "color": lid_color,
        },
    ])
    executor = CadQueryExecutor(
        runtime_name="podman",
        image_ref=os.environ["SANDBOX_IMAGE"],
        sandbox_command="podman",
    )
    outcome = await executor.execute(source, mode="3d", timeout_s=180)
    try:
        assert outcome.success, outcome.error_message
        import trimesh

        mesh = trimesh.load_mesh(
            outcome.files["result.stl"],
            force="mesh",
            process=False,
        )
        mesh.merge_vertices(digits_vertex=8)
        mesh.process(validate=True)
        assert mesh.is_watertight
        assert tuple(float(value) for value in mesh.bounding_box.extents) == (
            30.0,
            20.0,
            8.0,
        )
        assert abs(abs(float(mesh.volume)) - 4800.0) < 1e-6
    finally:
        if outcome.work_dir is not None:
            shutil.rmtree(outcome.work_dir, ignore_errors=True)
