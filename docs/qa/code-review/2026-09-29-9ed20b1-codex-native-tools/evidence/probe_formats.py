import json
import tempfile
from pathlib import Path
import cadquery as cq
from geometry_validation import validate_geometry_files

shape=cq.Solid.makeBox(20,20,20)
contract={'objective':'20 mm in X','checks':[{'check_id':'length','kind':'overall_dimension',
    'nominal':20,'required':True,'description':'length','source_quote':'20 mm',
    'scope':{'axis':[1,0,0]}}]}
rows=[]
with tempfile.TemporaryDirectory() as directory:
    for kind in ['stl','step']:
        file=Path(directory)/('model.'+kind);cq.exporters.export(shape,str(file))
        artifacts=[{'role':'model','format':kind,'path':str(file)}]
        rows.append({'format':kind,'without_acceptance':validate_geometry_files(artifacts),
                     'with_acceptance':validate_geometry_files(artifacts,acceptance=contract)})
print('FORMAT_REVIEW='+json.dumps(rows))
