"""Real FEA, CAM and release operations through the shared subprocess protocol.

Run with Python inside the sandbox image, with writable /sandbox/input/output.
"""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import zipfile

sys.path.insert(0, '/opt/cad-agent')
import capability_entry as dispatcher


def main():
    source=Path('/sandbox/input/beam.FCStd')
    fixture="""import FreeCAD as App, Part
doc=App.newDocument('ProtocolBeam')
body=doc.addObject('PartDesign::Body','Body')
solid=body.newObject('PartDesign::Feature','Solid')
solid.Shape=Part.makeBox(100,10,10)
doc.recompute()
doc.saveAs('/sandbox/input/beam.FCStd')
App.closeDocument(doc.Name)
"""
    subprocess.run(['/opt/freecad/bin/FreeCADCmd','-P','/opt/cad-agent','-c',
        'exec(compile('+repr(fixture)+", '<fixture>', 'exec'))"],check=True,
        cwd='/sandbox/output',stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    digest=hashlib.sha256(source.read_bytes()).hexdigest()
    cases=[
        {'kind':'linear_static','component_name':'Body','mesh_size_mm':5,
         'material':{'name':'Acceptance steel','young_modulus_mpa':210000,'poisson_ratio':0.3},
         'fixed_face':{'axis':'x','side':'min'},'loaded_face':{'axis':'x','side':'max'},'force_n':[1000,0,0]},
        {'kind':'contour_milling','component_name':'Body',
         'tool':{'name':'Flat mill','diameter_mm':6,'cutting_length_mm':12},
         'postprocessor':'grbl_1_1','work_origin_mm':[0,0,0],'stepdown_mm':3,
         'feed_mm_min':400,'plunge_mm_min':100,'spindle_rpm':12000,'safe_height_mm':5,
         'stock_margin_mm':5,'radial_allowance_mm':0,'chord_tolerance_mm':0.01},
        {'kind':'release_package','release_name':'Result channel regression',
         'source':{'document_id':'protocol-fixture','revision_id':'fixture-revision',
                   'state_version':1,'fcstd_sha256':digest},'annotations':[],'engineering_artifacts':[]},
    ]
    evidence=[]
    for params in cases:
        task={'schema_version':'mcad-capability-task.v1','capability':'freecad',
              'operation':'engineering','params':params,'inputs':{'base':'beam.FCStd'}}
        Path('/sandbox/input/task.json').write_text(json.dumps(task))
        files,metadata=dispatcher._freecad(task)
        assert metadata['result']['schema_version']=='freecad-engineering-result.v1'
        assert all(path.is_file() and path.stat().st_size for path in files.values())
        report=json.loads(files['engineering_report'].read_text())
        kind=params['kind']
        if kind=='linear_static':
            expected=1000*100/(210000*100)
            assert abs(report['mean_loaded_displacement_mm'][0]/expected-1)<.03
            assert report['force_balance_relative_error']<1e-4
        elif kind=='contour_milling':
            program=files['cam_program'].read_text()
            assert 'G21 G90 G17 G94 G40 G49 G80' in program and program.endswith('M5\nM2\n')
            assert report['passes']==4 and report['minimum_target_clearance_mm']>=0
        else:
            bom=json.loads(files['release_bom_json'].read_text())
            assert bom['generator']['native_type']=='Assembly::BomObject' and bom['instance_count']==1
            with zipfile.ZipFile(files['engineering_bundle']) as archive:
                manifest=json.loads(archive.read('manifest.json'))
                for item in manifest['files']:
                    data=archive.read(item['path'])
                    assert len(data)==item['size_bytes'] and hashlib.sha256(data).hexdigest()==item['sha256']
        assert hashlib.sha256(source.read_bytes()).hexdigest()==digest
        evidence.append({'kind':kind,'schema':metadata['result']['schema_version'],
            'roles':list(files),'source_unchanged':True})
        print('PASS',kind,flush=True)
    print('CAD_ENGINEERING_RESULT_CHANNEL='+json.dumps(evidence),flush=True)


if __name__=='__main__':main()
