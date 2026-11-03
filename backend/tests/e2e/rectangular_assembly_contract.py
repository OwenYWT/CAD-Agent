"""Actual native assembly, FCStd reopen, independent final STEP acceptance."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile

from geometry_validation import validate_geometry_files


cases=json.loads(Path('/sandbox/input/rectangular-assembly.json').read_text())
with tempfile.TemporaryDirectory() as directory:
    root=Path(directory)
    # Parameters here describe test geometry. The verifier never sees them.
    program=f'''
import FreeCAD as App, Part, json
root={str(root)!r}
cases={json.dumps([{'name':c['name'],'variant':c['variant'],'parameters':c['parameters']} for c in cases])}
for case in cases:
    p=case['parameters'];v=case['variant']
    L,W,H=p['length'],p['width'],p['height'];wall=2 if v=='wrong_wall' else 3
    floor=2 if v=='wrong_floor' else 3;lid_t=2 if v=='wrong_lid' else 3;body_h=H-3
    TL=p['tab_length']+(1 if v=='wrong_tab_length' else 0)
    TW=p['tab_width']+(1 if v=='wrong_tab_width' else 0)
    TT=2 if v=='wrong_tab_thickness' else 3
    GW=p['groove_width']-(.2 if v=='wrong_groove_width' else 0)
    GD=p['groove_depth']+(.2 if v=='wrong_groove_depth' else 0)
    shell=Part.makeBox(L,W,body_h).cut(Part.makeBox(L-2*wall,W-2*wall,body_h-floor,App.Vector(wall,wall,floor)))
    centers=[]
    for i,(x,y) in enumerate([(-TL,0),(-TL,W-TW),(L,0),(L,W-TW)]):
        if v=='missing_tab' and i==0:continue
        shell=shell.fuse(Part.makeBox(TL,TW,TT,App.Vector(x,y,0)))
        centers.append((x+TL/2,y+TW/2))
    margin=(wall-GW)/2
    groove=Part.makeBox(L-2*margin,W-2*margin,GD,App.Vector(margin,margin,body_h-GD)).cut(
        Part.makeBox(L-2*(margin+GW),W-2*(margin+GW),GD,App.Vector(margin+GW,margin+GW,body_h-GD)))
    if v=='broken_groove':groove=groove.cut(Part.makeBox(5,wall,GD,App.Vector(L/2,0,body_h-GD)))
    shell=shell.cut(groove)
    for i,(x,y) in enumerate(centers):
        if v=='missing_mount' and i==0:continue
        depth=TT-.1 if v=='blind_mount' and i==0 else TT
        radius=p['mount_diameter']/2+(.2 if v=='wrong_mount_diameter' and i==0 else 0)
        shell=shell.cut(Part.makeCylinder(radius,depth,App.Vector(x+(.3 if v=='off_center_mount' and i==0 else 0),y,0)))
    cr=p['connector_diameter']/2+(.2 if v=='wrong_connector_diameter' else 0)
    depth=wall-.1 if v=='blind_connector' else wall
    if v=='long_side_connector':
        connector=Part.makeCylinder(cr,depth,App.Vector(L/2,0,H/2),App.Vector(0,1,0))
    else:
        connector=Part.makeCylinder(cr,depth,App.Vector(0,W/2+(.4 if v=='off_center_connector' else 0),H/2),App.Vector(1,0,0))
    shell=shell.cut(connector)
    if v in ['unknown_micro_bore','unknown_nurbs_bore','inner_micro_bore']:
        cutter=Part.makeCylinder(.01,.5,App.Vector(L/2,wall-.5 if v=='inner_micro_bore' else 0,body_h/2),App.Vector(0,1,0))
        if v=='unknown_nurbs_bore':cutter=cutter.toNurbs()
        shell=shell.cut(cutter)
    if v=='tab_cavity_channel':
        shell=shell.cut(Part.makeBox(TL+wall,1,2,App.Vector(-TL/2,TW/2-.5,1)))
    lid=Part.makeBox(L,W,lid_t,App.Vector(.5 if v=='lid_shift' else 0,0,body_h+(.2 if v=='lid_gap' else 0)))
    shell=shell.removeSplitter()
    if v=='rotated':
        for shape in [shell,lid]:
            shape.rotate(App.Vector(),App.Vector(1,2,3),37);shape.translate(App.Vector(17,-9,11))
    assert shell.isValid() and lid.isValid() and len(shell.Solids)==len(lid.Solids)==1,case['name']
    doc=App.newDocument('Assembly')
    objects=[]
    for name,shape in [('Body',shell),('Lid',lid)]:
        body=doc.addObject('PartDesign::Body',name)
        feature=body.newObject('PartDesign::Feature',name+'Material');feature.Shape=shape
        objects.append(body)
    doc.recompute();doc.saveAs(root+'/'+case['name']+'.FCStd');App.closeDocument(doc.Name)
    reopened=App.openDocument(root+'/'+case['name']+'.FCStd')
    Part.export([reopened.getObject('Body'),reopened.getObject('Lid')],root+'/'+case['name']+'.step')
    App.closeDocument(reopened.Name)
print('CAD_ASSEMBLY_NATIVE_READY')
'''
    script=root/'build.py';script.write_text(program)
    command=f"exec(compile(open({str(script)!r}).read(), {str(script)!r}, 'exec'))"
    result=subprocess.run(['/opt/freecad/bin/FreeCADCmd','-c',command],capture_output=True,text=True,timeout=240)
    assert result.returncode==0 and 'CAD_ASSEMBLY_NATIVE_READY' in result.stdout,result.stdout+result.stderr
    reports=[]
    for case in cases:
        path=root/(case['name']+'.step');digest=hashlib.sha256(path.read_bytes()).hexdigest()
        context={'schema_version':'measurement-context.v1','base_revision_id':'assembly-source',
                 'checkpoint_manifest_id':'assembly-regression','artifact_sha256s':[digest]}
        report=validate_geometry_files([{'role':'model','format':'step','path':str(path)}],
            acceptance=case['contract'],measurement_context=context)
        if evidence_dir:=os.environ.get('CAD_ASSEMBLY_EVIDENCE_DIR'):
            import shutil
            for suffix in ['.FCStd','.step']:
                shutil.copyfile(root/(case['name']+suffix),Path(evidence_dir)/(case['name']+suffix))
            (Path(evidence_dir)/(case['name']+'.json')).write_text(json.dumps({**case,'report':report,'context':context}))
        assert (report['outcome']=='passed')==case['expected_pass'],(case['name'],report)
        assert hashlib.sha256(path.read_bytes()).hexdigest()==digest
        reports.append({**case,'report':report,'context':context})
        print('CAD_ASSEMBLY_CASE='+json.dumps({'name':case['name'],'outcome':report['outcome']}),flush=True)
    print('CAD_ASSEMBLY_REPORTS='+json.dumps(reports,ensure_ascii=False))
