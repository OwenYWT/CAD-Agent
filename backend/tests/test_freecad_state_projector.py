from __future__ import annotations

from types import SimpleNamespace

from app.freecad.state_projector import project_document


class Shape:
    ShapeType = "Solid"
    Faces = [1] * 6
    Edges = [1] * 12
    Solids = [1]
    Area = 600.0
    Volume = 1000.0

    @staticmethod
    def isNull() -> bool:
        return False


class Feature:
    Name = "Pad"
    Label = "Base pad"
    TypeId = "PartDesign::Pad"
    State = []
    InList = []
    OutList = []
    PropertiesList = ["Length", "Length2", "Shape"]
    Length = SimpleNamespace(UserString="10.00 mm", Value=10.0)
    Length2 = SimpleNamespace(UserString="5.00 mm", Value=5.0)
    Shape = Shape()

    @staticmethod
    def getTypeIdOfProperty(name: str) -> str:
        return (
            "App::PropertyLength"
            if name in {"Length", "Length2"}
            else "Part::PropertyPartShape"
        )

    @staticmethod
    def getEditorMode(name: str):
        return [] if name == "Length" else ["ReadOnly"]

    @staticmethod
    def getExpression(_name: str):
        return None

    @staticmethod
    def getGroupOfProperty(_name: str) -> str:
        return "Data"

    @staticmethod
    def isValid() -> bool:
        return True


def test_state_projector_emits_bounded_feature_tree_and_shape_facts() -> None:
    feature = Feature()
    document = SimpleNamespace(
        Name="Model",
        TopologicalSortedObjects=[feature],
        Objects=[feature],
        RootObjects=[feature],
    )

    state = project_document(document)

    assert state["schema_version"] == "freecad-state.v2"
    assert state["root_objects"] == ["Pad"]
    assert state["objects"][0]["properties"] == {
        "Length": "10.00 mm",
        "Length2": "5.00 mm",
    }
    assert state["objects"][0]["shape"] == {
        "type": "Solid",
        "faces": 6,
        "edges": 12,
        "solids": 1,
        "area": 600.0,
        "volume": 1000.0,
    }
    assert state["parameters"] == [{
        "id": "Pad.Length",
        "object_name": "Pad",
        "property_name": "Length",
        "label": "Base pad · Length",
        "group": "Data",
        "property_type": "App::PropertyLength",
        "value": 10.0,
        "unit": "mm",
        "editable": True,
        "minimum": None,
        "maximum": None,
        "step": None,
    }]


def test_expression_engine_without_get_expression_never_exposes_a_dependent_driver():
    from app.freecad.state_projector import project_parameters
    feature = Feature()
    feature.getExpression = None  # Actual FreeCAD 1.1.3 public API shape.
    feature.ExpressionEngine = [('Length', 'Source.Length / 2')]
    assert project_parameters(feature) == []
    feature.ExpressionEngine = []
    assert [p['id'] for p in project_parameters(feature)] == ['Pad.Length']


def test_body_membership_order_is_independent_of_dependencies_and_tip():
    from app.freecad.state_projector import project_object
    first, second = Feature(), Feature()
    first.Name, second.Name = "First", "Second"
    body = SimpleNamespace(Name="Body", TypeId="PartDesign::Body", Group=[second, first], Tip=first,
                           OutList=[first, second], InList=[])
    projected = project_object(body)
    assert projected["out"] == ["First", "Second"]
    assert projected["structure"] == {"status": "measured", "category": "body", "members": ["Second", "First"], "body_tip": "First"}
    del body.Group
    assert project_object(body)["structure"]["status"] == "unavailable"
