"""Run using FreeCADCmd and a real saved FCStd supplied as argv/environment.

Exercises the old checkpoint -> new projection -> save/reopen boundary without
changing the source checkpoint. No LLM or geometry execution is substituted.
"""
import hashlib
import json
import os
import sys

sys.path.insert(0, '/opt/cad-agent')
import FreeCAD as App
import Part
from freecad_state_projector import project_document, project_object
from freecad_reference_geometry import REFERENCE_KINDS


def main():
    source = os.environ['CAD_REFERENCE_TEST_FCSTD']
    before_hash = hashlib.sha256(open(source, 'rb').read()).hexdigest()
    doc = App.openDocument(source)
    try:
        physical = [obj for obj in doc.Objects if obj.TypeId == 'PartDesign::Body']
        assert physical
        before = {
            obj.Name: (obj.Shape.Volume, obj.Shape.BoundBox.XLength, obj.Shape.BoundBox.YLength, obj.Shape.BoundBox.ZLength)
            for obj in physical}
        refs = [obj for obj in doc.Objects if obj.TypeId in REFERENCE_KINDS]
        assert refs
        state = project_document(doc)
        by_name = {obj['name']: obj for obj in state['objects']}
        for ref in refs:
            obj = by_name[ref.Name]
            assert obj['reference_geometry']['extent'] == 'unbounded'
            assert 'shape' not in obj
            assert obj['inspection']['topology']['status'] == 'not_applicable'
            assert obj['geometry_sha256'] and obj['global_placement']
        doc.saveAs('/sandbox/output/reference-roundtrip.FCStd')
        App.closeDocument(doc.Name)
        doc = App.openDocument('/sandbox/output/reference-roundtrip.FCStd')
        after = {obj.Name: (obj.Shape.Volume, obj.Shape.BoundBox.XLength,
                          obj.Shape.BoundBox.YLength, obj.Shape.BoundBox.ZLength)
                 for obj in doc.Objects if obj.TypeId == 'PartDesign::Body'}
        assert before == after
        assert hashlib.sha256(open(source, 'rb').read()).hexdigest() == before_hash
        # Names must never classify physical geometry as a reference.
        other = App.newDocument('PhysicalReferenceName')
        try:
            obj = other.addObject('Part::Feature', 'Y_Axis')
            obj.Shape = Part.makeBox(5, 7, 9)
            projected = project_object(obj)
            assert 'reference_geometry' not in projected
            assert abs(projected['shape']['volume'] - 315) < 1e-8
        finally:
            App.closeDocument(other.Name)
        print('CAD_REFERENCE_GEOMETRY=' + json.dumps({'references': len(refs),
            'physical_measurements': before, 'source_sha256': before_hash}), flush=True)
    finally:
        App.closeDocument(doc.Name)


if __name__ == '__main__':
    try:
        main()
    except BaseException:
        import traceback
        traceback.print_exc()
        os._exit(1)
