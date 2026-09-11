"""Check real GRBL paths against independently swept native cutter solids."""
import hashlib
import json
from pathlib import Path
import re
import sys
import tempfile

sys.path[:0]=['/engineering-source','/opt/cad-agent']
import FreeCAD as App
import Part
from freecad_engineering import run_engineering,EngineeringError


with tempfile.TemporaryDirectory() as root:
    root=Path(root);inputs=root/'input';inputs.mkdir()
    params={'kind':'contour_milling','component_name':'Body','tool':{'name':'6 mm flat end mill','diameter_mm':6,'cutting_length_mm':12},
        'postprocessor':'grbl_1_1','work_origin_mm':[5,-2,1],'stepdown_mm':3,'feed_mm_min':400,'plunge_mm_min':100,
        'spindle_rpm':12000,'safe_height_mm':5,'stock_margin_mm':5,'radial_allowance_mm':0,'chord_tolerance_mm':0.01}
    reports=[]
    for index,shape in enumerate([Part.makeBox(40,20,8),Part.makeCylinder(10,8),
        Part.makeBox(40,20,8).cut(Part.makeCylinder(3,8,App.Vector(20,10,0))),
        Part.makeBox(40,20,8).cut(Part.makeBox(10,10,8,App.Vector(30,10,0)))]):
        document=App.newDocument('CAM'+str(index));body=document.addObject('PartDesign::Body','Body')
        feature=body.newObject('PartDesign::Feature','Solid');feature.Shape=shape;document.recompute()
        source=inputs/f'part{index}.FCStd';document.saveAs(str(source));App.closeDocument(document.Name)
        digest=hashlib.sha256(source.read_bytes()).hexdigest();output=root/f'out{index}';output.mkdir()
        result=run_engineering({'params':params,'inputs':{'base':source.name}},inputs,output)
        report=json.loads(Path(result['files']['engineering_report']).read_text())
        field=json.loads(Path(result['files']['engineering_field']).read_text())
        program=Path(result['files']['cam_program']).read_text()
        assert 'G21 G90 G17 G94 G40 G49 G80' in program and program.endswith('M5\nM2\n')
        assert report['passes']==3 and report['depths_mm']==[4,1,-1] and report['minimum_target_clearance_mm']>=0
        assert report['internal_loops_not_machined']==(1 if index==2 else 0)
        previous={};moves=[]
        for line in program.splitlines():
            match=re.match(r'^(G0|G1) ',line)
            if not match:continue
            current={**previous,**{axis:float(value) for axis,value in re.findall(r'([XYZ])(-?\d+(?:\.\d+)?)',line)}}
            if all(a in current for a in 'XYZ'):
                moves.append((match[1],App.Vector(*(current[a]+params['work_origin_mm'][i] for i,a in enumerate('XYZ')))))
            previous=current
        assert len(moves)==len(field['trajectory'])
        for i,(motion,b) in enumerate(moves[1:],1):
            a=moves[i-1][1]
            if motion=='G0':
                assert (abs(a.x-b.x)<1e-7 and abs(a.y-b.y)<1e-7) or min(a.z,b.z)>=shape.BoundBox.ZMax+5-1e-7
                continue
            # Independently sweep a cylinder's XY footprint, including both
            # caps, through each emitted segment at the cutter's axial depth.
            delta=b-a
            if abs(delta.z)>1e-7:
                swept=Part.makeCylinder(3,abs(delta.z)+12,App.Vector(b.x,b.y,min(a.z,b.z)))
            else:
                cutter_face=Part.Face(Part.Wire(Part.makeCircle(3,App.Vector(a.x,a.y,b.z))))
                start_cylinder=cutter_face.extrude(App.Vector(0,0,12))
                end_cylinder=Part.makeCylinder(3,12,App.Vector(b.x,b.y,b.z))
                if delta.Length<1e-9:swept=start_cylinder
                else:
                    normal=App.Vector(-delta.y,delta.x,0);normal.normalize();normal*=3
                    corners=[a+normal,b+normal,b-normal,a-normal,a+normal]
                    swept=start_cylinder.fuse(end_cylinder).fuse(Part.Face(Part.makePolygon(corners)).extrude(App.Vector(0,0,12)))
            assert swept.common(shape).Volume<1e-7,(index,i,motion)
        assert hashlib.sha256(source.read_bytes()).hexdigest()==digest
        reports.append({'shape':index,'passes':report['passes'],'segments':report['segments'],'clearance_mm':report['minimum_target_clearance_mm']})
    bad=Part.makeBox(40,20,4).fuse(Part.makeBox(20,20,4,App.Vector(0,0,4)))
    from freecad_cam import contour_milling
    for shape,request in [(bad,params),(Part.makeBox(40,20,20),params)]:
        try:contour_milling(shape,request,root)
        except EngineeringError as error:assert error.code in {'cam_profile_unsupported','cam_tool_reach'}
        else:raise AssertionError('unsupported setup was accepted')
    print('CAD_CAM_ACCEPTANCE='+json.dumps({'cases':reports,'actual_swept_cutter_clearance':True,'nc_coordinates_and_origin_verified':True,
        'rapid_moves_verified':True,'stepped_shape_and_short_tool_rejected':True,'fcstd_unchanged':True}),flush=True)
