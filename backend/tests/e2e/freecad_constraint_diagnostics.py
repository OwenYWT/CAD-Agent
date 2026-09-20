"""Run with the real FreeCADCmd: classify solver errors before generic validity."""
import json
import sys

sys.path.insert(0, '/opt/cad-agent')
import FreeCAD as App
import Part
import Sketcher
import freecad_entry as runner


def main():
    checks=[]
    for radius in (3.5, 8.0):
        for case in ('normal', 'under', 'redundant', 'conflicting'):
            doc=App.newDocument('ConstraintRegression')
            try:
                body=doc.addObject('PartDesign::Body', 'Body')
                sketch=body.newObject('Sketcher::SketchObject', 'ArbitraryProfile')
                sketch.addGeometry(Part.Circle(App.Vector(11,17,0),App.Vector(0,0,1),radius),False)
                if case!='under':
                    sketch.addConstraint(Sketcher.Constraint('DistanceX',0,3,11.0))
                    sketch.addConstraint(Sketcher.Constraint('DistanceY',0,3,17.0))
                    sketch.addConstraint(Sketcher.Constraint('Radius',0,radius))
                if case in ('redundant','conflicting'):
                    sketch.addConstraint(Sketcher.Constraint('Radius',0,radius if case=='redundant' else radius+2))
                doc.recompute()
                expected={'redundant':'sketch_redundant_constraints',
                          'conflicting':'sketch_conflicting_constraints','under':'sketch_under_constrained'}.get(case)
                try:
                    runner._validate_document(doc,op_id='diagnostic-operation',action='sketch.add_constraint')
                    runner._require_fully_constrained(sketch)
                except runner.FreeCADRunnerError as exc:
                    assert exc.code==expected,(case,exc.code,str(exc))
                    assert exc.details['object']==sketch.Name
                    assert 'degrees_of_freedom' in exc.details
                    if case!='under':
                        assert exc.op_id=='diagnostic-operation'
                        assert exc.details[case+'_constraint_numbers']
                    checks.append({'case':case,'radius':radius,'code':exc.code,'details':exc.details})
                else:
                    assert expected is None,case
                    checks.append({'case':case,'radius':radius,'status':'valid'})
            finally:
                App.closeDocument(doc.Name)
    print('CAD_CONSTRAINT_DIAGNOSTICS='+json.dumps(checks),flush=True)


if __name__=='__main__':
    try:main()
    except BaseException:
        import os,traceback
        traceback.print_exc();os._exit(1)
