"""Independent STEP measurement. Offline container entry point."""
import json
import math
import sys
from pathlib import Path
import cadquery as cq
from cq_reference import tests

def score(step, case):
    model=cq.importers.importStep(str(step))
    metadata=json.loads((case/'case.json').read_text())
    scale=metadata['linear_scale_mm_per_source_unit']
    if scale!=1:model=cq.Workplane().newObject([s.scale(1/scale) for s in model.vals()])
    shapes=model.vals()
    solids=[s for shape in shapes for s in shape.Solids()]
    validity=bool(solids) and all(s.isValid() and s.Volume()>0 for s in solids)
    checks=json.loads((case/'checks.json').read_text())
    verdict=tests(model,checks)
    groups={}
    for c in verdict['checks']:
        if c['role']=='required':groups.setdefault(c['requirement_id'] or str(c['id']),[]).append(c['status']=='pass')
    b=model.val().BoundingBox()
    return {'schema_version':'cad-eval-score.v1','passed':validity and verdict['passed'],
        'geometry_valid':validity,'solid_count':len(solids),'volume_mm3':sum(s.Volume() for s in solids)*scale**3,
        'bbox_mm':[b.xlen*scale,b.ylen*scale,b.zlen*scale],'requirements_passed':sum(all(v) for v in groups.values()),
        'requirements_total':len(groups),'checks':verdict['checks'],
        'scorer_error':any(c['status']=='error' and c['role']=='required' for c in verdict['checks'])}

if __name__=='__main__':
    try: result=score(Path(sys.argv[1]),Path(sys.argv[2]))
    except Exception as exc:result={'passed':False,'scorer_error':True,'error':type(exc).__name__+': '+str(exc)}
    Path(sys.argv[3]).write_text(json.dumps(result,indent=2))
    print(json.dumps({'passed':result['passed'],'scorer_error':result['scorer_error']}))
