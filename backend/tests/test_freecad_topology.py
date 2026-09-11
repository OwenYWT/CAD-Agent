from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.freecad.topology import TopologyResolutionError, resolve_topology_selector
from app.topology.contracts import FreeCADTopologySelector


@dataclass
class _Vector:
    x: float
    y: float
    z: float


class _Face:
    def __init__(self, *, center: _Vector, normal: _Vector, area: float):
        self.CenterOfMass = center
        self._normal = normal
        self.Area = area
        self.Surface = SimpleNamespace(TypeId="Part::GeomPlane")

    def normalAt(self, _u, _v):
        return self._normal


class _Circle:
    def __init__(self, *, radius: float, center: _Vector, axis: _Vector):
        self.Radius = radius
        self.Center = center
        self.Axis = axis


class _Edge:
    def __init__(self, curve):
        self.Curve = curve


class _Shape:
    def __init__(self, *, faces=(), edges=()):
        self.Faces = list(faces)
        self.Edges = list(edges)


class _Object:
    def __init__(self, name: str, shape: _Shape):
        self.Name = name
        self.Shape = shape

    def resolveSubElement(self, name, append, mode):
        assert append is False
        assert mode == 0
        return self, name, name


class _Document:
    def __init__(self, obj: _Object):
        self._obj = obj

    def getObject(self, name: str):
        return self._obj if name == self._obj.Name else None


def _selector(revision_id, **overrides):
    payload = {
        "schema_version": "topology-selector.v1",
        "backend": "freecad",
        "revision_id": revision_id,
        "object_name": "Pad",
        "subelement_kind": "face",
        "geometry": "planar",
        "axis": "z",
        "extreme": "max",
        "normal_sign": 1,
    }
    payload.update(overrides)
    return FreeCADTopologySelector.model_validate(payload)


def test_planar_selector_re_resolves_after_topology_order_changes() -> None:
    first_revision = uuid4()
    first = _Object(
        "Pad",
        _Shape(
            faces=[
                _Face(center=_Vector(5, 0, 5), normal=_Vector(1, 0, 0), area=200),
                _Face(center=_Vector(5, 5, 10), normal=_Vector(0, 0, 1), area=100),
            ]
        ),
    )
    selector = _selector(first_revision)
    first_result = resolve_topology_selector(
        _Document(first),
        selector.model_dump(mode="json"),
        expected_revision_id=str(first_revision),
    )

    second_revision = uuid4()
    changed = _Object(
        "Pad",
        _Shape(
            faces=[
                _Face(center=_Vector(5, 5, 20), normal=_Vector(0, 0, 1), area=100),
                _Face(center=_Vector(5, 0, 10), normal=_Vector(1, 0, 0), area=400),
            ]
        ),
    )
    updated_selector = selector.model_copy(update={"revision_id": second_revision})
    second_result = resolve_topology_selector(
        _Document(changed),
        updated_selector.model_dump(mode="json"),
        expected_revision_id=str(second_revision),
    )

    assert first_result["subelement_name"] == "Face2"
    assert second_result["subelement_name"] == "Face1"
    assert second_result["signature"]["position_mm"] == 20
    assert second_result["resolution_method"] == "geometry+resolveSubElement"
    assert "Face" not in selector.model_dump_json()


def test_circular_selector_uses_radius_axis_and_extreme() -> None:
    revision_id = uuid4()
    obj = _Object(
        "Pad",
        _Shape(
            edges=[
                _Edge(_Circle(radius=4, center=_Vector(50, 30, 0), axis=_Vector(0, 0, 1))),
                _Edge(_Circle(radius=4, center=_Vector(50, 30, 10), axis=_Vector(0, 0, 1))),
            ]
        ),
    )
    selector = _selector(
        revision_id,
        subelement_kind="edge",
        geometry="circular",
        radius_mm=4,
        normal_sign=None,
    )

    result = resolve_topology_selector(
        _Document(obj),
        selector.model_dump(mode="json"),
        expected_revision_id=str(revision_id),
    )

    assert result["subelement_name"] == "Edge2"
    assert result["signature"]["radius_mm"] == 4
    assert result["signature"]["position_mm"] == 10


def test_selector_rejects_raw_subelement_and_stale_revision() -> None:
    revision_id = uuid4()
    payload = _selector(revision_id).model_dump(mode="json")
    payload["subelement_name"] = "Face6"
    with pytest.raises(ValidationError, match="subelement_name"):
        FreeCADTopologySelector.model_validate(payload)

    obj = _Object(
        "Pad",
        _Shape(
            faces=[
                _Face(center=_Vector(0, 0, 10), normal=_Vector(0, 0, 1), area=100)
            ]
        ),
    )
    with pytest.raises(TopologyResolutionError, match="revision"):
        resolve_topology_selector(
            _Document(obj),
            _selector(revision_id).model_dump(mode="json"),
            expected_revision_id=str(uuid4()),
        )


def test_planar_selector_rejects_axis_aligned_normal_on_curved_surface():
    revision = str(uuid4())
    face = _Face(center=_Vector(0, 0, 5), normal=_Vector(1, 0, 0), area=314)
    face.Surface = SimpleNamespace(TypeId="Part::GeomCylinder")
    with pytest.raises(TopologyResolutionError, match="no planar face"):
        resolve_topology_selector(_Document(_Object("Pad", _Shape(faces=[face]))),
                                  _selector(revision, axis="x").model_dump(mode="json"),
                                  expected_revision_id=revision)


def test_planar_selector_does_not_use_unrequested_area_to_break_ties():
    revision = str(uuid4())
    faces = [_Face(center=_Vector(x, 0, 5), normal=_Vector(0, 0, 1), area=area)
             for x, area in [(0, 100), (20, 28)]]
    with pytest.raises(TopologyResolutionError, match="ambiguous"):
        resolve_topology_selector(_Document(_Object("Pad", _Shape(faces=faces))),
                                  _selector(revision).model_dump(mode="json"),
                                  expected_revision_id=revision)
