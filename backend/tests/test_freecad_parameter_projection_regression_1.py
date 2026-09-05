from __future__ import annotations

from types import SimpleNamespace

from app.freecad.state_contract import (
    FreeCADStateV2,
    compile_parameter_operation_plan,
)
from app.freecad.state_projector import project_parameters


# Regression: FUSION-005 — generic numeric-property discovery exposed Hole
# implementation fields and dropped real Chamfer/Fillet dimensions.


class _Quantity:
    def __init__(self, value: float) -> None:
        self.Value = value
        self.UserString = f"{value} mm"

    def getValueAs(self, unit: str):
        assert unit == "mm"
        return SimpleNamespace(Value=self.Value)


class _Feature:
    def __init__(
        self,
        *,
        name: str,
        label: str,
        type_id: str,
        properties: dict[str, tuple[str, object]],
        **values: object,
    ) -> None:
        self.Name = name
        self.Label = label
        self.TypeId = type_id
        self.PropertiesList = list(properties)
        self._properties = properties
        for property_name, (_property_type, value) in properties.items():
            setattr(self, property_name, value)
        for property_name, value in values.items():
            setattr(self, property_name, value)

    def getTypeIdOfProperty(self, name: str) -> str:
        return self._properties[name][0]

    @staticmethod
    def getEditorMode(_name: str):
        return []

    @staticmethod
    def getExpression(_name: str):
        return None

    @staticmethod
    def getGroupOfProperty(_name: str) -> str:
        return "Data"


def test_projection_exposes_only_typed_feature_semantics() -> None:
    hole = _Feature(
        name="Hole",
        label="Through hole",
        type_id="PartDesign::Hole",
        properties={
            "Diameter": ("App::PropertyLength", _Quantity(8)),
            "Depth": ("App::PropertyLength", _Quantity(10)),
            "BaseProfileType": ("App::PropertyInteger", 7),
            "ThreadDepth": ("App::PropertyLength", _Quantity(372)),
        },
        DepthType=1,
    )
    chamfer = _Feature(
        name="Chamfer",
        label="Edge chamfer",
        type_id="PartDesign::Chamfer",
        properties={
            "Size": ("App::PropertyQuantityConstraint", _Quantity(1)),
        },
    )

    hole_parameters = project_parameters(hole)
    chamfer_parameters = project_parameters(chamfer)

    assert [parameter["id"] for parameter in hole_parameters] == ["Hole.Diameter"]
    assert [parameter["id"] for parameter in chamfer_parameters] == ["Chamfer.Size"]
    assert chamfer_parameters[0]["property_type"] == "App::PropertyQuantityConstraint"
    assert chamfer_parameters[0]["unit"] == "mm"


def test_quantity_constraint_round_trips_through_typed_modify_contract() -> None:
    state = {
        "schema_version": "freecad-state.v2",
        "document": "Model",
        "object_count": 1,
        "root_objects": ["Chamfer"],
        "objects": [],
        "parameters": [
            {
                "id": "Chamfer.Size",
                "object_name": "Chamfer",
                "property_name": "Size",
                "label": "Edge chamfer · Size",
                "group": "Data",
                "property_type": "App::PropertyQuantityConstraint",
                "value": 1.0,
                "unit": "mm",
                "editable": True,
                "minimum": None,
                "maximum": None,
                "step": None,
            }
        ],
    }

    FreeCADStateV2.model_validate(state)
    plan = compile_parameter_operation_plan(
        state,
        {"parameter_updates": [{"parameter_id": "Chamfer.Size", "value": 1.5}]},
        output_formats=("step",),
    )

    assert plan.operations[0].args == {
        "object": "Chamfer",
        "property": "Size",
        "value": 1.5,
        "expected_property_type": "App::PropertyQuantityConstraint",
        "unit": "mm",
    }
