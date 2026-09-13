"""Create two actual independent FreeCAD bodies through the durable V1 kernel path.

Arguments are an already registered acceptance user's UUID and owned project
UUID. The normal queue, Temporal worker, sandbox, S3 and review/commit are used.
"""
import asyncio
import json
import sys
from uuid import UUID,uuid4

from app.db import tenant_transaction,close_database
from app.domain.identity import user_principal
from app.repositories.revisions import create_initial_branch
from app.services.change_sets import accept_change_set,commit_change_set
from app.workflows.temporal import start_mcad_workflow,McadExecutionRequest,McadOutputRequest
from app.freecad.contracts import FreeCADOperationPlan

OUTPUTS={'fcstd':'application/vnd.freecad.fcstd','state':'application/json','step':'model/step','stl':'model/stl'}


def _operation(op_id,action,args):
    return {'op_id':op_id,'action':action,'args':args}


def _circle_operations(prefix,sketch,x,y,radius):
    return [_operation(prefix+'-circle','sketch.add_geometry',{'sketch':sketch,
        'geometry':{'kind':'circle','center':{'x':x,'y':y},'radius_mm':radius}}),
        *[_operation(prefix+'-'+kind,'sketch.add_constraint',{'sketch':sketch,'kind':kind,
            'first':{'geometry_index':0,**({'point_position':3} if kind!='radius' else {})},'value_mm':value})
          for kind,value in [('distance_x',x),('distance_y',y),('radius',radius)]]]


def _plan(name,operations):
    return FreeCADOperationPlan.model_validate({'schema_version':'freecad-operation-plan.v1',
        'document_name':name,'operations':operations}).model_dump(mode='json')


async def main():
    UUID(sys.argv[1])  # Validate the argument while preserving the auth subject's exact spelling.
    owner=user_principal(sys.argv[1]);project_id=UUID(sys.argv[2])
    async with tenant_transaction(owner.tenant_id,owner.principal_id) as conn:
        initial=await create_initial_branch(conn,tenant_id=owner.tenant_id,project_id=project_id,
            created_by_principal_id=owner.principal_id,branch_name='collaboration-'+uuid4().hex[:10],initial_manifest={})
    operations=[]
    for name,x in [('A',5),('B',25)]:
        operations.append(_operation('create-'+name.lower(),'sketch.create',{'name':'Sketch'+name,'body':'Body'+name,'plane':'xy'}))
        operations.extend(_circle_operations(name.lower(),'Sketch'+name,x,5,5))
        operations.append(_operation('pad-'+name.lower(),'feature.pad',{'name':'Pad'+name,'profile':'Sketch'+name,'length_mm':10}))
    if '--triangle' in sys.argv[3:]:
        points=[{'x':5,'y':5},{'x':8,'y':5},{'x':8,'y':9}]
        operations=[_operation('create-triangle','sketch.create',{'name':'SketchA','body':'BodyA','plane':'xy'})]
        for i in range(3):
            operations.append(_operation(f'line-{i}','sketch.add_geometry',{'sketch':'SketchA',
                'geometry':{'kind':'line','start':points[i],'end':points[(i+1)%3]}}))
        for i in range(3):
            operations.append(_operation(f'join-{i}','sketch.add_constraint',{'sketch':'SketchA','kind':'coincident',
                'first':{'geometry_index':i,'point_position':2},'second':{'geometry_index':(i+1)%3,'point_position':1}}))
        operations.append(_operation('horizontal','sketch.add_constraint',{'sketch':'SketchA','kind':'horizontal','first':{'geometry_index':0}}))
        for axis in ('x','y'):
            operations.append(_operation(f'origin-{axis}','sketch.add_constraint',{'sketch':'SketchA','kind':'distance_'+axis,
                'first':{'geometry_index':0,'point_position':1},'value_mm':5}))
        for i,length in enumerate((3,4,5)):
            operations.append(_operation(f'length-{i}','sketch.add_constraint',{'sketch':'SketchA','kind':'distance',
                'first':{'geometry_index':i},'value_mm':length}))
        operations.append(_operation('pad-triangle','feature.pad',{'name':'PadA','profile':'SketchA','length_mm':10}))
    if '--instance' in sys.argv[3:]:
        operations.append(_operation('instance-a','assembly.instance',{'object':'InstanceA','source':'BodyA','translation_mm':[45,0,0]}))
    if '--four-holes' in sys.argv[3:]:
        from app.freecad.operation_compiler import compile_common_generation
        plate=compile_common_generation({'part_type':'plate','dimensions':{'length':60,'width':40,'thickness':8},
            'features':['through_hole:diameter=6,position=centered'],'constraints':[]},output_formats=('step','stl'))
        assert plate is not None
        operations=[op.model_dump(mode='json') for op in plate.operations[:-1]]
        hole_index=next(i for i,op in enumerate(operations) if op['action']=='feature.hole')
        additions=[]
        for index,(x,y) in enumerate([(25,20),(55,20),(25,40)],start=1):
            additions.append(_operation(f'multi-circle-{index}','sketch.add_geometry',{'sketch':'HoleSketch',
                'geometry':{'kind':'circle','center':{'x':x,'y':y},'radius_mm':3}}))
            for kind,value in [('distance_x',x),('distance_y',y),('radius',3)]:
                additions.append(_operation(f'multi-{index}-{kind}','sketch.add_constraint',{'sketch':'HoleSketch',
                    'kind':kind,'first':{'geometry_index':index,**({'point_position':3} if kind!='radius' else {})},'value_mm':value}))
        operations[hole_index:hole_index]=additions
    if '--many-instances' in sys.argv[3:]:
        for index in range(100):
            operations.append(_operation(f'instance-{index}','assembly.instance',{'object':f'Instance{index:03}',
                'source':'BodyA','translation_mm':[45+(index%10)*20,(index//10)*20,0]}))
    triangle='--triangle' in sys.argv[3:]
    operations.append(_operation('export','document.export',{'formats':['fcstd','step','stl'],'basename':'triangle' if triangle else 'two_bodies'}))
    name='MultiHolePlate' if '--four-holes' in sys.argv[3:] else 'ConstraintTriangle' if triangle else 'TwoBodies'
    task={'schema_version':'mcad-capability-task.v1','capability':'freecad','operation':'execute',
          'params':{'plan':_plan(name,operations)},'inputs':{}}
    primary=McadExecutionRequest(step_key='two-bodies',kind='mcad_model',capability='mcad.freecad',operation='execute',
        source_language='json',source_code=json.dumps(task),timeout_seconds=180,
        outputs=tuple(McadOutputRequest(name=k,media_type=v) for k,v in OUTPUTS.items()))
    workflow_id,handle=await start_mcad_workflow(tenant_id=owner.tenant_id,project_id=project_id,principal_id=owner.principal_id,
        branch_id=initial.branch_id,expected_base_revision_id=initial.revision_id,kind='mcad.execute',idempotency_key='two-bodies-'+str(initial.branch_id),
        primary=primary,objective='One native Hole feature with four measured profiles' if '--four-holes' in sys.argv[3:] else 'Fully constrained 3/4/5 mm triangular prism for solver failure acceptance' if triangle else 'Two independent native cylinders for real collaboration acceptance',require_confirmation=False,commit_after_confirmation=False)
    assert handle is not None
    result=await asyncio.wait_for(handle.result(),timeout=180)
    assert result['status']=='succeeded',result
    change=UUID(result['change_set_id'])
    await accept_change_set(tenant_id=owner.tenant_id,reviewer_principal_id=owner.principal_id,change_set_id=change,review_note='Real native geometry and artifacts inspected')
    committed=await commit_change_set(tenant_id=owner.tenant_id,reviewer_principal_id=owner.principal_id,change_set_id=change)
    assert committed.status=='committed'
    print('CAD_COLLABORATION_DOCUMENT='+json.dumps({'document_id':str(initial.branch_id),'project_id':str(project_id),
        'workflow_id':str(workflow_id),'tenant_id':str(owner.tenant_id)}))
    await close_database()


asyncio.run(main())
