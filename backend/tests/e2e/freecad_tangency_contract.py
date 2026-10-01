"""Compare a native feature chain with independent OCCT booleans at exact tangency."""
import json
import sys
import tempfile
from pathlib import Path
sys.path.insert(0,'/opt/cad-agent')
import freecad_entry as runner
App,Part=runner.App,runner.Part

# The cylindrical post meets the bottom over an area, but its side only
# touches the interior wall at a line. No tolerance or dimensions are changed.
box=Part.makeBox(30,24,15)
cavity=Part.makeBox(26,20,13,App.Vector(2,2,2))
post=Part.makeCylinder(2.5,6,App.Vector(4.5,4.5,2))
shape=box.cut(cavity).fuse(post).removeSplitter()
try:
    errors=shape.check(True)
    direct_error=str(errors) if errors else ''
except ValueError as exc:
    direct_error=str(exc)
assert 'SelfIntersect' in direct_error, direct_error

with tempfile.TemporaryDirectory() as root:
    root=Path(root);runner.INPUT_ROOT=root/'input';runner.INPUT_ROOT.mkdir()
    runner.OUTPUT_ROOT=root/'output';runner.OUTPUT_ROOT.mkdir()
    operations=[]
    def add(action,**args):
        operations.append({'op_id':f'op-{len(operations)}','action':action,'args':args})
    for name,offset,geometry in [
        ('Base',0,{'kind':'rectangle','corner':{'x':0,'y':0},'width_mm':30,'height_mm':24}),
        ('Cavity',15,{'kind':'rectangle','corner':{'x':2,'y':2},'width_mm':26,'height_mm':20}),
        ('Post',2,{'kind':'circle','center':{'x':4.5,'y':4.5},'radius_mm':2.5})]:
        add('sketch.create',name=name,offset_mm=offset)
        add('sketch.add_profile',sketch=name,geometry=geometry)
        if name=='Base':add('feature.pad',name='Pad',profile=name,length_mm=15)
        if name=='Cavity':add('feature.pocket',name='CavityCut',profile=name,length_mm=13)
    add('feature.pad',name='PostPad',profile='Post',length_mm=6)
    add('document.export',formats=['fcstd','step'],basename='tangent')
    try:
        runner.run_task({'schema_version':'mcad-capability-task.v1','capability':'freecad','operation':'execute',
            'inputs':{},'params':{'plan':{'schema_version':'freecad-operation-plan.v1','document_name':'Tangency','operations':operations}}})
    except runner.FreeCADRunnerError as exc:
        assert exc.code=='shape_check_failed' and 'SelfIntersect' in str(exc)
        assert exc.action=='feature.pad' and exc.details.get('feature')=='PostPad', exc.details
    else:
        raise AssertionError('Invalid exact tangency was accepted')
    assert not list(runner.OUTPUT_ROOT.glob('*.FCStd'))
print('CAD_TANGENCY_CONTRACT='+json.dumps({'native_and_direct_occt':'SelfIntersect','invalid_candidate_not_exported':True}))
