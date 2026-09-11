"""Verify a real native multi-body/linked BOM and every packaged byte."""
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import zipfile
import csv

sys.path[:0]=['/engineering-source','/opt/cad-agent']
import FreeCAD as App
import Part
from freecad_release import run_release

with tempfile.TemporaryDirectory() as root:
    root=Path(root);inputs=root/'input';inputs.mkdir();output=root/'output';output.mkdir()
    document=App.newDocument('ReleaseFixture')
    first=document.addObject('PartDesign::Body','BodyA');solid=first.newObject('PartDesign::Feature','SolidA');solid.Shape=Part.makeBox(20,10,8)
    first.Label='=UntrustedLabel'
    second=document.addObject('PartDesign::Body','BodyB');solid=second.newObject('PartDesign::Feature','SolidB');solid.Shape=Part.makeCylinder(3,10,App.Vector(30,0,0))
    link=document.addObject('App::Link','InstanceA');link.setLink(first);link.Placement.Base=App.Vector(50,0,0)
    document.recompute();source=inputs/'source.FCStd';document.saveAs(str(source));App.closeDocument(document.Name)
    digest=hashlib.sha256(source.read_bytes()).hexdigest()
    params={'kind':'release_package','release_name':'Native release acceptance','source':{'document_id':'release-fixture','revision_id':'revision-fixture',
        'state_version':1,'fcstd_sha256':digest},'annotations':[],'engineering_artifacts':[]}
    result=run_release({'params':params,'inputs':{'base':source.name}},inputs,output)
    report=json.loads(Path(result['files']['engineering_report']).read_text())
    bom=json.loads(Path(result['files']['release_bom_json']).read_text())
    assert bom['generator']['native_type']=='Assembly::BomObject'
    assert bom['instance_count']==3 and bom['definition_count']==2 and sum(r['quantity'] for r in bom['rows'])==3,bom
    assert {r['kernel_name'] for r in bom['rows']}=={'BodyA','BodyB','InstanceA'}
    with zipfile.ZipFile(result['files']['engineering_bundle']) as bundle:
        manifest=json.loads(bundle.read('manifest.json'))
        for entry in manifest['files']:
            raw=bundle.read(entry['path'])
            assert len(raw)==entry['size_bytes'] and hashlib.sha256(raw).hexdigest()==entry['sha256']
        assert hashlib.sha256(bundle.read('design.FCStd')).hexdigest()==digest
        rows=list(csv.reader(io.StringIO(bundle.read('bom.csv').decode())))
        assert any(row[1]=="'=UntrustedLabel" for row in rows[1:]),rows
    imported=Part.Shape();imported.read(result['files']['release_step'])
    assert len(imported.Solids)==3 and abs(imported.Volume-report['total_component_volume_mm3'])<1e-5
    assert hashlib.sha256(source.read_bytes()).hexdigest()==digest
    print('CAD_RELEASE_KERNEL='+json.dumps({'native_bom':True,'instances':3,'definitions':2,'step_solids':len(imported.Solids),
        'every_packaged_hash_verified':True,'source_fcstd_unchanged':True,'csv_formula_label_escaped':True}),flush=True)
