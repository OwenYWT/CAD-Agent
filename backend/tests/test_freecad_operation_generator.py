from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.agent.durable_plan import AffectedObject, AgentPlan, AgentPlanStep
from app.freecad.operation_generator import FreeCADOperationGenerator
from app.models.schemas import DesignBrief


def _plan() -> AgentPlan:
    return AgentPlan(
        objective="Create a 10 mm cylinder",
        operation="generate",
        model_kind="simple",
        modeling_strategy="freecad_operations",
        design_brief=DesignBrief(
            intent_summary="Cylinder",
            artifact_type="custom",
        ),
        affected_objects=(
            AffectedObject(
                object_id="part-main",
                object_type="part",
                label="Cylinder",
                change="create",
            ),
        ),
        steps=(
            AgentPlanStep(
                step_key="model-main",
                kind="model",
                description="Cylinder",
                affected_object_ids=("part-main",),
                output_formats=("step", "stl"),
            ),
        ),
    )


def _valid_plan() -> dict:
    return {
        "schema_version": "freecad-operation-plan.v1",
        "document_name": "Cylinder",
        "operations": [
            {
                "op_id": "create-sketch",
                "action": "sketch.create",
                "args": {"name": "BaseSketch", "plane": "xy"},
            },
            {
                "op_id": "add-circle",
                "action": "sketch.add_geometry",
                "args": {
                    "sketch": "BaseSketch",
                    "geometry": {
                        "kind": "circle",
                        "center": {"x": 5, "y": 5},
                        "radius_mm": 5,
                    },
                },
            },
            {
                "op_id": "pad",
                "action": "feature.pad",
                "args": {
                    "name": "Pad",
                    "profile": "BaseSketch",
                    "length_mm": 10,
                },
            },
            {
                "op_id": "export",
                "action": "document.export",
                "args": {
                    "formats": ["fcstd", "step", "stl"],
                    "basename": "model",
                },
            },
        ],
    }


class _Completions:
    def __init__(self, payloads: list[dict]):
        self.payloads = list(payloads)
        self.calls = 0
        self.kwargs: list[dict] = []

    async def create(self, **kwargs):
        self.kwargs.append(kwargs)
        payload = self.payloads[self.calls]
        self.calls += 1
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    finish_reason="stop",
                    message=SimpleNamespace(content=json.dumps(payload)),
                )
            ]
        )


def _client(payloads: list[dict]):
    completions = _Completions(payloads)
    return SimpleNamespace(
        chat=SimpleNamespace(completions=completions),
        completions=completions,
    )


def _provenance() -> dict:
    return {
        "provider": "test-provider",
        "model": "test-model",
        "provider_response_id": "response-1",
        "request_hash": "a" * 64,
        "response_hash": "b" * 64,
        "finish_reason": "stop",
        "usage": {},
    }


@pytest.mark.asyncio
async def test_generator_preserves_rejection_reason_after_exhausting_repairs():
    reason='unsupported: 没有提供具体的零件描述、尺寸或需继续的设计目标'
    generator=FreeCADOperationGenerator(client=_client([{'error':reason},{'error':reason}]), provenance_reader=_provenance)
    with pytest.raises(ValueError, match='没有提供具体的零件描述') as caught:
        await generator.generate(plan=_plan(),requirements={'description':'继续'},
            base_state=None,output_formats=('step','stl'))
    assert str(caught.value.__cause__)==reason


def _plate_state() -> dict:
    return {
        "schema_version": "freecad-state.v1",
        "document": "Model",
        "object_count": 3,
        "objects": [
            {
                "name": "Body",
                "type_id": "PartDesign::Body",
                "properties": {"Tip": "Hole"},
            },
            {
                "name": "Pad",
                "type_id": "PartDesign::Pad",
                "properties": {"Length": "10.00 mm"},
                "shape": {"solids": 1},
            },
            {
                "name": "Hole",
                "type_id": "PartDesign::Hole",
                "properties": {"Diameter": "6.00 mm"},
                "shape": {"solids": 1},
            },
        ],
    }


@pytest.mark.asyncio
async def test_generator_retries_contract_error_and_returns_typed_plan() -> None:
    client = _client([{"error": "unsupported"}, _valid_plan()])
    generator = FreeCADOperationGenerator(
        client=client,
        provenance_reader=_provenance,
    )

    result = await generator.generate(
        plan=_plan(),
        requirements={"description": "Cylinder"},
        base_state=None,
        output_formats=("step", "stl"),
    )

    assert client.completions.calls == 2
    assert result.generator_kind == "freecad_operations"
    assert result.operation_plan.operations[-1].args["formats"] == [
        "fcstd",
        "step",
        "stl",
    ]
    assert json.loads(result.source_code)["schema_version"] == (
        "freecad-operation-plan.v1"
    )
    assert "max_tokens" not in client.completions.kwargs[0]


@pytest.mark.asyncio
async def test_compiles_verified_revolved_circle_as_cylinder_without_provider() -> None:
    client = _client([])
    generator = FreeCADOperationGenerator(client=client)

    result = await generator.generate(
        plan=_plan(),
        requirements={
            "part_type": "revolution",
            "dimensions": {
                "width": 20,
                "depth": 20,
                "diameter": 20,
                "height": 30,
            },
            "features": [
                "revolve_profile:circle_diameter=20,height=30,centered",
                "fully_constrained_sketch:true",
            ],
            "constraints": ["草图必须完全约束"],
        },
        base_state=None,
        output_formats=("step", "stl"),
    )

    assert client.completions.calls == 0
    assert result.generator_kind == "freecad_operations_compiled"
    pad = next(
        item.typed_args()
        for item in result.operation_plan.operations
        if item.action == "feature.pad"
    )
    circle = next(
        item.typed_args().geometry
        for item in result.operation_plan.operations
        if item.action == "sketch.add_geometry"
    )
    assert (circle.radius_mm, pad.length_mm) == (10, 30)


@pytest.mark.asyncio
async def test_compiles_structured_base_cylinder_metadata_without_provider() -> None:
    client = _client([])
    generator = FreeCADOperationGenerator(client=client)

    result = await generator.generate(
        plan=_plan(),
        requirements={
            "part_type": "cylinder",
            "dimensions": {"diameter": 20, "height": 30},
            "features": ["base_cylinder:diameter=20,height=30"],
            "constraints": ["sketch_fully_constrained=true"],
        },
        base_state=None,
        output_formats=("step", "stl"),
    )

    assert client.completions.calls == 0
    assert result.generator_kind == "freecad_operations_compiled"


@pytest.mark.asyncio
async def test_compiles_observed_cylinder_contract_without_provider() -> None:
    client = _client([])
    generator = FreeCADOperationGenerator(client=client)

    result = await generator.generate(
        plan=_plan(),
        requirements={
            "part_type": "cylinder",
            "dimensions": {"diameter": 20, "radius": 10, "height": 30},
            "features": [],
            "constraints": ["sketch_fully_constrained"],
        },
        base_state=None,
        output_formats=("step", "stl"),
    )

    assert client.completions.calls == 0
    assert result.generator_kind == "freecad_operations_compiled"


@pytest.mark.asyncio
async def test_compiles_observed_cylinder_metadata_with_cross_checks() -> None:
    client = _client([])
    generator = FreeCADOperationGenerator(client=client)

    result = await generator.generate(
        plan=_plan(),
        requirements={
            "part_type": "cylinder",
            "dimensions": {
                "width": 20,
                "depth": 20,
                "diameter": 20,
                "height": 30,
            },
            "features": [
                "cylinder:solid,diameter=20,height=30",
                "sketch:fully_constrained",
            ],
            "constraints": [
                "diameter = 20 mm",
                "height = 30 mm",
                "sketch_fully_constrained = true",
            ],
        },
        base_state=None,
        output_formats=("step", "stl"),
    )

    assert client.completions.calls == 0
    assert result.generator_kind == "freecad_operations_compiled"


@pytest.mark.asyncio
async def test_inconsistent_cylinder_radius_fails_closed_to_provider() -> None:
    client = _client([_valid_plan()])
    generator = FreeCADOperationGenerator(
        client=client,
        provenance_reader=_provenance,
    )

    result = await generator.generate(
        plan=_plan(),
        requirements={
            "part_type": "cylinder",
            "dimensions": {"diameter": 20, "radius": 12, "height": 30},
            "features": [],
            "constraints": ["sketch_fully_constrained"],
        },
        base_state=None,
        output_formats=("step", "stl"),
    )

    assert client.completions.calls == 1
    assert result.generator_kind == "freecad_operations"


@pytest.mark.asyncio
async def test_mismatched_cylinder_constraint_fails_closed_to_provider() -> None:
    client = _client([_valid_plan()])
    generator = FreeCADOperationGenerator(
        client=client,
        provenance_reader=_provenance,
    )

    result = await generator.generate(
        plan=_plan(),
        requirements={
            "part_type": "cylinder",
            "dimensions": {"diameter": 20, "height": 30},
            "features": ["cylinder:solid,diameter=20,height=30"],
            "constraints": ["diameter = 24 mm"],
        },
        base_state=None,
        output_formats=("step", "stl"),
    )

    assert client.completions.calls == 1
    assert result.generator_kind == "freecad_operations"


@pytest.mark.asyncio
async def test_generator_rejects_export_format_drift() -> None:
    invalid = _valid_plan()
    invalid["operations"][-1]["args"]["formats"] = ["fcstd", "step"]
    generator = FreeCADOperationGenerator(
        client=_client([invalid, invalid]),
        provenance_reader=_provenance,
    )

    with pytest.raises(ValueError, match="valid plan"):
        await generator.generate(
            plan=_plan(),
            requirements={"description": "Cylinder"},
            base_state=None,
            output_formats=("step", "stl"),
        )


@pytest.mark.asyncio
async def test_compiles_real_planner_plate_shape_without_provider() -> None:
    client = _client([])
    generator = FreeCADOperationGenerator(client=client)

    result = await generator.generate(
        plan=_plan(),
        requirements={
            "part_type": "plate",
            "dimensions": {"width": 100, "height": 60, "depth": 10},
            "features": [
                "through_hole:diameter=6,count=1,position=centered",
                "plate:thickness=10",
                "fillet:radius=2,edges=all_outer",
            ],
            "constraints": ["hole_diameter = 6", "hole_centered_on_face"],
        },
        base_state=None,
        output_formats=("step", "stl"),
    )

    assert client.completions.calls == 0
    assert result.generator_kind == "freecad_operations_compiled"
    operations = {item.action: item.typed_args() for item in result.operation_plan.operations}
    assert operations["feature.pad"].length_mm == 10
    assert operations["feature.fillet"].radius_mm == 2
    assert operations["feature.hole"].diameter_mm == 6
    assert operations["document.export"].formats == ("fcstd", "step", "stl")


@pytest.mark.asyncio
async def test_compiles_hole_change_and_chamfer_against_observed_state() -> None:
    plan = _plan().model_copy(update={"operation": "modify"})
    client = _client([])
    generator = FreeCADOperationGenerator(client=client)

    result = await generator.generate(
        plan=plan,
        requirements={
            "description": "Change the centered hole to 8 mm and add a 1 mm chamfer",
            "modification_type": "dimension_change",
            "target_params": {"hole_diameter": 8},
            "new_features": ["1 mm chamfer on all outer edges"],
        },
        base_state=_plate_state(),
        output_formats=("step",),
    )

    assert client.completions.calls == 0
    assert result.generator_kind == "freecad_operations_compiled"
    actions = [item.action for item in result.operation_plan.operations]
    assert actions == ["property.set", "feature.chamfer", "document.export"]
    property_args = result.operation_plan.operations[0].typed_args()
    assert (property_args.object, property_args.property, property_args.value) == (
        "Hole",
        "Diameter",
        8.0,
    )
    chamfer_args = result.operation_plan.operations[1].typed_args()
    assert (chamfer_args.target, chamfer_args.size_mm) == ("Hole", 1.0)
    assert chamfer_args.use_all_edges is False
    assert chamfer_args.edge_scope == "outer"
    assert result.operation_plan.operations[0].op_id != (
        result.operation_plan.operations[1].op_id
    )


@pytest.mark.asyncio
async def test_compiles_planner_dimensions_without_using_hole_as_thickness() -> None:
    client = _client([])
    generator = FreeCADOperationGenerator(client=client)

    result = await generator.generate(
        plan=_plan(),
        requirements={
            "part_type": "plate",
            "dimensions": {
                "width": 60,
                "length": 100,
                "thickness": 10,
                "hole_diameter": 6,
            },
            "features": [
                "base_plate:length=100,width=60,thickness=10",
                "extrude:profile=rectangle,length=100,width=60,thickness=10",
                "extrusion:width=100,depth=60,height=10",
                "through_hole:diameter=6,count=1,position=centered",
            ],
            "constraints": [
                "hole_diameter < plate_width",
                "hole_centered_on_plate",
            ],
        },
        base_state=None,
        output_formats=("step", "stl"),
    )

    assert client.completions.calls == 0
    pad = next(
        item.typed_args()
        for item in result.operation_plan.operations
        if item.action == "feature.pad"
    )
    rectangle = next(
        item.typed_args().geometry
        for item in result.operation_plan.operations
        if item.action == "sketch.add_geometry"
        and item.args["geometry"]["kind"] == "rectangle"
    )
    assert (rectangle.width_mm, rectangle.height_mm, pad.length_mm) == (
        100,
        60,
        10,
    )
