"""Read-only component extraction from the exact final native checkpoint.

This fixed program never recomputes, saves, repairs or edits the document. The
independent geometry worker subsequently proves containment in the final STEP.
"""
import hashlib
import json
import os
from pathlib import Path

import FreeCAD as App
from freecad_scene import component_shapes


def main():
    request_path=Path(os.environ['CAD_MEASUREMENT_REQUEST'])
    request=json.loads(request_path.read_text())
    source=Path(request['input_path'])
    if (source.is_symlink() or source.resolve().parent!=Path('/sandbox/input')
            or source.suffix.lower()!='.fcstd'):
        raise ValueError('measurement requires a declared native checkpoint')
    original=hashlib.sha256(source.read_bytes()).hexdigest()
    if original!=request['sha256']:
        raise ValueError('native measurement checkpoint digest differs')
    document=App.openDocument(str(source))
    try:
        components={obj.Name:(obj,shape) for obj,shape in component_shapes(document)}
        result={}
        for index,reference in enumerate(request['objects']):
            if reference['kernel_name'] not in components:
                result[reference['feature_id']]={'status':'missing','reason':'selected_object_missing','reference':reference}
                continue
            obj,world=components[reference['kernel_name']]
            if obj.TypeId!=reference['type_id'] or not world.isValid():
                raise ValueError('selected native component changed type or is invalid')
            if obj.TypeId=='App::Link' and (float(getattr(obj,'Scale',1))!=1
                    or any(abs(v-1)>1e-12 for v in getattr(obj,'ScaleVector',(1,1,1)))):
                raise ValueError('scaled links have no certified rigid coordinate equivalence')
            local=world.copy()
            transform=list(local.Placement.toMatrix().A)
            local.Placement=App.Placement()
            paths={}
            for frame,shape in [('world',world),('local',local)]:
                path=request_path.parent/f'{index}-{frame}.brep'
                shape.exportBrep(str(path))
                paths[frame]={'filename':path.name,'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}
            result[reference['feature_id']]={'reference':reference,'frame_transform':transform,'shapes':paths}
        (request_path.parent/'components.json').write_text(json.dumps(result,sort_keys=True))
    finally:
        App.closeDocument(document.Name)
    if hashlib.sha256(source.read_bytes()).hexdigest()!=original:
        raise ValueError('read-only measurement changed its native input')
    print('CAD_MEASUREMENT_SOURCE_READONLY=1')


if __name__=='__main__':
    main()
