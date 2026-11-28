from __future__ import annotations

from types import SimpleNamespace


class _Shape:
    def __init__(self, *, null=False):
        self._null = null

    def isNull(self):
        return self._null


class _Object:
    def __init__(self, name, type_id, *, visible=True, parents=()):
        self.Name = name
        self.TypeId = type_id
        self.Shape = _Shape()
        self.InList = list(parents)
        self.ViewObject = SimpleNamespace(Visibility=visible)

    def isDerivedFrom(self, type_id):
        return type_id == "Part::Feature" and self.TypeId in {
            "PartDesign::Body",
            "PartDesign::Feature",
            "Part::Feature",
        }


def test_native_selection_excludes_consumed_history_bodies():
    from benchmark.cad_test_engine.native_delivery_selection import select_final_objects

    final = _Object("Final", "PartDesign::Body")
    history_a = _Object("HistoryA", "PartDesign::Body", visible=False, parents=(final,))
    history_b = _Object("HistoryB", "PartDesign::Body", visible=False, parents=(final,))

    selected = select_final_objects([history_a, history_b, final])

    assert [item.Name for item in selected] == ["Final"]


def test_native_selection_keeps_independent_visible_assembly_bodies():
    from benchmark.cad_test_engine.native_delivery_selection import select_final_objects

    body = _Object("Body", "PartDesign::Body")
    lid = _Object("Lid", "PartDesign::Body")

    selected = select_final_objects([body, lid])

    assert [item.Name for item in selected] == ["Body", "Lid"]


def test_native_delivery_script_returns_nonzero_for_failed_verdict():
    script = (
        __file__
        .replace("tests\\test_native_delivery_selection.py", "benchmark\\cad_test_engine\\check_native_delivery.py")
    )
    source = open(script, encoding="utf-8").read()

    assert "raise SystemExit(0 if result['passed'] else 1)" in source
