"""Compare actual quadratic tetrahedral CalculiX solves with beam mechanics."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile

sys.path[:0]=['/engineering-source','/opt/cad-agent']
import FreeCAD as App
import Part
from freecad_engineering import run_engineering, EngineeringError

with tempfile.TemporaryDirectory() as root:
    root=Path(root);inputs=root/'input';inputs.mkdir()
    doc=App.newDocument('EngineeringBeam');body=doc.addObject('PartDesign::Body','Body')
    feature=body.newObject('PartDesign::Feature','Solid');feature.Shape=Part.makeBox(100,10,10)
    doc.recompute();source=inputs/'beam.FCStd';doc.saveAs(str(source));App.closeDocument(doc.Name)
    digest=hashlib.sha256(source.read_bytes()).hexdigest()
    def run(name,force):
        output=root/name;output.mkdir()
        result=run_engineering({'params':{'kind':'linear_static','component_name':'Body','mesh_size_mm':5,
            'material':{'name':'Acceptance steel','young_modulus_mpa':210000,'poisson_ratio':0.3},
            'fixed_face':{'axis':'x','side':'min'},'loaded_face':{'axis':'x','side':'max'},'force_n':force},
            'inputs':{'base':'beam.FCStd'}},inputs,output)
        report=json.loads(Path(result['files']['engineering_report']).read_text())
        field=json.loads(Path(result['files']['engineering_field']).read_text())
        assert report['element_type']=='C3D10' and report['nodes']>100 and report['elements']>100
        assert report['force_balance_relative_error']<1e-4
        assert len(field['positions_mm'])==len(field['displacements_mm'])==len(field['von_mises_mpa'])==report['nodes']
        assert field['triangles'] and Path(result['files']['engineering_bundle']).stat().st_size>10000
        assert hashlib.sha256(source.read_bytes()).hexdigest()==digest
        print(name,report,flush=True)
        return report
    first=run('tension',[1000,0,0])
    expected=1000*100/(210000*100)
    assert abs(first['mean_loaded_displacement_mm'][0]/expected-1)<0.03,first
    second=run('double-tension',[2000,0,0])
    assert abs(second['mean_loaded_displacement_mm'][0]/first['mean_loaded_displacement_mm'][0]-2)<1e-4
    bending=run('bending',[0,100,0])
    expected=100*100**3/(3*210000*(10*10**3/12))
    assert abs(bending['mean_loaded_displacement_mm'][1]/expected-1)<0.10,bending
    print('CAD_FEA_ACCEPTANCE='+json.dumps({'actual_gmsh_calculix':True,'quadratic_tetrahedra':True,
        'axial_solution_within_3_percent':True,'force_scaling_verified':True,'bending_solution_within_10_percent':True,
        'reaction_balance_verified':True,'input_fcstd_unchanged':True}),flush=True)
