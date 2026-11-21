"""Offline analytic cases through the existing typed FreeCAD runner.

`run` freezes each recipe, expected values and exact operations before execution;
`replay` consumes those bytes without regenerating them. These are tool/kernel
tests, not natural-language Agent success rates. No provider is called.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
import math
import os
from pathlib import Path
import random
import subprocess
import time
from uuid import uuid4


SCHEMA = 'native-oracle-case.v1'
GENERATOR = 'perforated-plate.v1'


def sha(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                     ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n')


def cross(a, b):
    return [a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2], a[0]*b[1]-a[1]*b[0]]


def world(case, x, y, z):
    frame = case['frame']; y_axis = cross(frame['normal'], frame['x_axis'])
    return [frame['origin'][i]+x*frame['x_axis'][i]+y*y_axis[i]+z*frame['normal'][i] for i in range(3)]


def validate_case(case):
    if case.get('schema_version') != SCHEMA or case.get('kind') != 'perforated_plate':
        raise ValueError('unsupported native oracle recipe')
    w, d, h = case['size_mm']
    numbers = [w,d,h,*case['frame']['origin'],*case['frame']['normal'],*case['frame']['x_axis']]
    for hole in case['holes']:
        numbers.extend(hole[k] for k in ('x','y','radius','depth'))
    if any(type(v) not in (int,float) or not math.isfinite(v) for v in numbers) or min(w,d,h) <= 0:
        raise ValueError('finite positive dimensions required')
    n, x = case['frame']['normal'], case['frame']['x_axis']
    if (any(len(v) != 3 for v in (n,x,case['frame']['origin']))
            or abs(sum(v*v for v in n)-1) > 1e-9 or abs(sum(v*v for v in x)-1) > 1e-9
            or abs(sum(a*b for a,b in zip(n,x))) > 1e-9):
        raise ValueError('orthonormal frame required')
    ids = set()
    for index, hole in enumerate(case['holes']):
        r = hole['radius']
        if (hole['id'] in ids or r <= 0 or not r < hole['x'] < w-r or not r < hole['y'] < d-r
                or not 0 < hole['depth'] <= h or type(hole['through']) is not bool
                or hole['through'] != (hole['depth'] == h)):
            raise ValueError('hole must be wholly inside the plate with an explicit valid depth')
        ids.add(hole['id'])
        if any(math.hypot(hole['x']-other['x'], hole['y']-other['y']) <= r+other['radius']
               for other in case['holes'][:index]):
            raise ValueError('analytic holes must not overlap')
    return case


def expected(case):
    """Closed-form material and surface properties; never calls a CAD kernel."""
    validate_case(case)
    w,d,h = case['size_mm']; volume = w*d*h
    moment = [volume*v for v in world(case,w/2,d/2,h/2)]
    area = 2*(w*d+w*h+d*h)
    for hole in case['holes']:
        r, depth = hole['radius'], hole['depth']
        removed = math.pi*r*r*depth
        center = world(case,hole['x'],hole['y'],h-depth/2)
        moment = [m-removed*c for m,c in zip(moment,center)]
        volume -= removed
        area += 2*math.pi*r*depth - (2*math.pi*r*r if hole['through'] else 0)
    corners = [world(case,x,y,z) for x in (0,w) for y in (0,d) for z in (0,h)]
    return {'formula_version': GENERATOR, 'solid_count': 1, 'volume_mm3': volume, 'area_mm2': area,
            'centroid_mm': [m/volume for m in moment],
            'bounds_mm': {'min': [min(p[i] for p in corners) for i in range(3)],
                          'max': [max(p[i] for p in corners) for i in range(3)]},
            'holes': [{'id': hole['id'], 'radius_mm': hole['radius'], 'depth_mm': hole['depth'],
                       'axis': case['frame']['normal'], 'top_mm': world(case,hole['x'],hole['y'],h),
                       'bottom_mm': world(case,hole['x'],hole['y'],h-hole['depth']),
                       'through': hole['through']} for hole in case['holes']]}


def compile_plan(case):
    validate_case(case)
    operations = []
    def add(op_id, action, **args):
        operations.append({'op_id': op_id, 'action': action, 'args': args})
    frame = case['frame']; w,d,h = case['size_mm']
    def native_frame(z):
        return {'origin': dict(zip('xyz',world(case,0,0,z))), 'normal': frame['normal'], 'x_axis': frame['x_axis']}
    add('base-sketch','sketch.create',name='Profile',frame=native_frame(0))
    add('base-profile','sketch.add_profile',sketch='Profile',geometry={'kind':'rectangle',
        'corner':{'x':0,'y':0},'width_mm':w,'height_mm':d})
    add('base-pad','feature.pad',name='Pad',profile='Profile',length_mm=h)
    for i,hole in enumerate(case['holes']):
        sketch, feature = f'Profile{i}', f'Hole{i}'
        add(hole['id']+'-sketch','sketch.create',name=sketch,frame=native_frame(h))
        add(hole['id']+'-profile','sketch.add_profile',sketch=sketch,geometry={'kind':'circle',
            'center':{'x':hole['x'],'y':hole['y']},'radius_mm':hole['radius']})
        add(hole['id']+'-cut','feature.hole',name=feature,profile=sketch,
            diameter_mm=2*hole['radius'],
            **({'through_all':True} if hole['through'] else {'depth_mm':hole['depth']}))
    add('export','document.export',formats=['fcstd','step'],basename='model')
    return {'schema_version':'freecad-operation-plan.v1','document_name':'Oracle','operations':operations}


def cases(seeds=()):
    base = {'schema_version':SCHEMA,'kind':'perforated_plate','case_id':'offset-through',
        'size_mm':[40,30,12], 'frame':{'origin':[0,0,0],'normal':[0,0,1],'x_axis':[1,0,0]},
        'holes':[{'id':'hole-0','x':11,'y':9,'radius':3,'depth':12,'through':True}]}
    result = [base]
    for name in ('plain','blind','translated','rotated','scaled','two-holes'):
        case = deepcopy(base); case['case_id'] = name
        if name == 'plain':case['holes'] = []
        if name == 'blind':case['holes'][0].update(depth=6,through=False)
        if name == 'translated':case['frame']['origin'] = [-27,19,-8]
        if name == 'rotated':case['frame'].update(origin=[5,-13,7],normal=[0,-1,0],x_axis=[1,0,0])
        if name == 'scaled':
            case['size_mm'] = [v*1.7 for v in case['size_mm']]
            for hole in case['holes']:
                for key in ('x','y','radius','depth'):hole[key] *= 1.7
        if name == 'two-holes':case['holes'].append({'id':'hole-1','x':29,'y':21,'radius':2,'depth':12,'through':True})
        result.append(case)
    for seed in seeds:
        rng = random.Random(seed)
        case = deepcopy(base); case.update(case_id=f'seed-{seed}',seed=seed,generator_version=GENERATOR)
        w,d,h = [rng.uniform(20,100),rng.uniform(20,80),rng.uniform(3,20)]
        r = rng.uniform(.3,min(w,d)/8)
        case['size_mm'] = [w,d,h]
        case['holes'][0].update(x=rng.uniform(2*r,w-2*r),y=rng.uniform(2*r,d-2*r),radius=r,depth=h)
        if rng.choice((True,False)):case['holes'][0].update(depth=h*rng.uniform(.2,.8),through=False)
        case['frame']['origin'] = [rng.uniform(-50,50) for _ in range(3)]
        case['frame']['normal'],case['frame']['x_axis'] = rng.choice((([0,0,1],[1,0,0]),([0,-1,0],[1,0,0]),([1,0,0],[0,1,0])))
        result.append(case)
    return result


def freeze(case, directory, operations=None):
    from app.freecad.contracts import FreeCADOperationPlan
    directory.mkdir(parents=True, exist_ok=False)
    plan = operations if operations is not None else compile_plan(case)
    FreeCADOperationPlan.model_validate(plan)
    oracle = expected(case)
    write_json(directory/'case.json',case); write_json(directory/'operations.json',plan)
    write_json(directory/'expected.json',oracle)
    identity = {'schema_version':'native-oracle-input.v1', 'case_sha256':sha(case),
                'operations_sha256':sha(plan),'expected_sha256':sha(oracle)}
    write_json(directory/'input.json',identity)
    # Match the existing sandbox staging boundary: the kernel runs as a
    # different UID. Its input mount is read-only; the private parent is kept.
    for name in ('case.json','operations.json','expected.json','input.json'):
        (directory/name).chmod(0o644)
    directory.chmod(0o755)
    return identity


def read_frozen(directory):
    values = [json.loads((directory/name).read_text()) for name in ('case.json','operations.json','expected.json','input.json')]
    for key,value in zip(('case','operations','expected'),values):
        if sha(value) != values[3][key+'_sha256']:
            raise ValueError('frozen '+key+' bytes changed')
    # The formula is independent of the operation list, including fault replays.
    if values[2] != expected(values[0]):raise ValueError('analytic reference differs from the frozen recipe')
    return values


def compare(oracle, measurements):
    """Complete, finite measurements only. Assertion identities survive replay."""
    if not isinstance(measurements,dict):return ['measurements.missing']
    failures = []
    def check(name, actual, wanted, tolerance):
        if type(actual) not in (int,float) or not math.isfinite(actual) or abs(actual-wanted) > tolerance:
            failures.append(name)
    for key,absolute in (('solid_count',0),('volume_mm3',1e-5),('area_mm2',1e-5)):
        wanted = oracle[key];check(key,measurements.get(key),wanted,absolute+abs(wanted)*1e-7)
    for key,wanted in [('centroid_mm',oracle['centroid_mm']),('bounds_min',oracle['bounds_mm']['min']),('bounds_max',oracle['bounds_mm']['max'])]:
        values = measurements.get(key)
        for i,value in enumerate(wanted):check(f'{key}.{i}',values[i] if isinstance(values,list) and len(values)==3 else None,value,1e-6)
    holes = measurements.get('holes')
    if not isinstance(holes,list):return failures+['holes.missing']
    check('hole_count',len(holes),len(oracle['holes']),0)
    used = set()
    for hole in oracle['holes']:
        axis,top = hole['axis'],hole['top_mm']
        def matching(actual):
            if not isinstance(actual,dict):return False
            center = actual.get('center_mm',[]); actual_axis = actual.get('axis',[])
            if len(center)!=3 or len(actual_axis)!=3 or not all(type(v) in (int,float) and math.isfinite(v) for v in center+actual_axis):return False
            delta = [a-b for a,b in zip(center,top)]
            return (math.sqrt(sum(v*v for v in cross(delta,axis))) < 1e-6
                    and abs(abs(sum(a*b for a,b in zip(axis,actual_axis)))-1) < 1e-7)
        hits = [(i,actual) for i,actual in enumerate(holes) if matching(actual)]
        if len(hits)!=1 or hits[0][0] in used:
            failures.append(hole['id']+'.identity');continue
        index,actual = hits[0];used.add(index)
        for key in ('radius_mm','depth_mm'):check(hole['id']+'.'+key,actual.get(key),hole[key],1e-6)
        ends = actual.get('ends_mm',[])
        for side in ('top_mm','bottom_mm'):
            if not any(isinstance(p,list) and len(p)==3 and all(type(v) in (int,float) and math.isfinite(v) for v in p)
                       and math.dist(p,hole[side]) < 1e-6 for p in ends):
                failures.append(hole['id']+'.'+side)
        if actual.get('through') is not hole['through']:failures.append(hole['id']+'.through')
    if measurements.get('valid') is not True:failures.append('shape.valid')
    return failures


def fingerprint(report):
    return {key:report.get(key) for key in ('status','error_code','diagnosis','failed_action','failed_op_id',
                                          'failed_assertions','runtime_image_digest')}


def execute_frozen(directory, *, runtime, image, timeout=120):
    root = Path(__file__).resolve().parents[2]
    case,plan,oracle,identity = read_frozen(directory)
    image_id = subprocess.check_output([runtime,'image','inspect','--format','{{.Id}}',image],text=True).strip()
    output = directory/'output';output.mkdir()
    output.chmod(0o777)
    name = 'cad-oracle-'+uuid4().hex
    command = [runtime,'run','--rm','--pull=never','--name',name,'--network','none','--read-only',
        '--cap-drop','ALL','--security-opt','no-new-privileges','--memory','2g','--cpus','2','--pids-limit','256',
        '--tmpfs','/tmp:rw,mode=1777,size=1g','-e',f'CAD_ORACLE_INVOCATION={name}','-v',f'{directory.resolve()}:/case:ro',
        '-v',f'{output.resolve()}:/sandbox/output','-v',f'{root}/backend/benchmark:/oracle:ro',
        '--entrypoint','/opt/freecad/bin/FreeCADCmd',image_id,'-c',
        "exec(compile(open('/oracle/native_oracle_worker.py').read(), '/oracle/native_oracle_worker.py', 'exec'))"]
    started = time.monotonic()
    with (directory/'runtime.log').open('w') as log:
        try:
            result = subprocess.run(command,stdout=log,stderr=subprocess.STDOUT,timeout=timeout,check=False)
            report_path = output/'report.json'
            if result.returncode or not report_path.is_file():
                report = {'status':'crash' if result.returncode in (134,137,139) else 'infrastructure_error',
                          'error_code':'native_report_missing','returncode':result.returncode}
            else:
                report = json.loads(report_path.read_text())
                if report.get('input') != identity or report.get('invocation') != name:
                    raise ValueError('native report does not match this invocation and frozen inputs')
                for source, target in (
                    ('backend/sandbox/freecad_entry.py', 'freecad_entry.py'),
                    ('backend/app/freecad/native_references.py', 'freecad_native_references.py'),
                    ('backend/app/contracts/native_references.py', 'freecad_native_reference_contracts.py'),
                    ('backend/app/freecad/state_projector.py', 'freecad_state_projector.py'),
                ):
                    if report.get('runtime_source_sha256', {}).get(target) != hashlib.sha256((root/source).read_bytes()).hexdigest():
                        raise ValueError('runtime source differs from this checkout: '+source)
                if report.get('status') == 'passed':
                    failed = [kind+'.'+failure for kind in ('fcstd','step')
                              for failure in compare(oracle,report.get('measurements',{}).get(kind,{}))]
                    if failed:report.update(status='failed',failed_assertions=failed)
                if report.get('measurements'):
                    roles = [artifact['role'] for artifact in report.get('artifacts',[])]
                    if any(roles.count(role)!=1 for role in ('fcstd','step','state')):
                        raise ValueError('native readback artifacts are missing or duplicated')
                for artifact in report.get('artifacts',[]):
                    path = output/artifact['filename']
                    if (path.parent != output or path.is_symlink() or not path.is_file()
                            or hashlib.sha256(path.read_bytes()).hexdigest() != artifact['sha256']):
                        raise ValueError('native output identity mismatch')
        except subprocess.TimeoutExpired:
            report = {'status':'timeout','error_code':'native_timeout'}
        except (ValueError,KeyError,TypeError,OSError) as exc:
            report = {'status':'infrastructure_error','error_code':'native_evidence_invalid','message':str(exc)}
        finally:
            subprocess.run([runtime,'rm','-f',name],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=15,check=False)
    report.update(case_id=case['case_id'],input=identity,elapsed_seconds=time.monotonic()-started,
                  execution_path='typed_freecad_runner',runtime_image_digest=image_id,model_calls=0)
    write_json(directory/'report.json',report)
    return report


def without_dependents(plan, removed_ids):
    """Delete explicit consumers with their prerequisites; replay checks implicit CAD dependencies."""
    operations=plan['operations'];removed=set(removed_ids);missing=set()
    changed=True
    while changed:
        changed=False
        for op in operations:
            args=op['args'];action=op['action']
            if op['op_id'] in removed:
                if action=='sketch.create' or action.startswith('feature.'):missing.add(args['name'])
                continue
            refs=[args[key] for key in ('sketch','profile','target','object','source','path') if isinstance(args.get(key),str)]
            refs.extend(args.get('profiles',[]));refs.extend(args.get('originals',[]))
            if any(ref in missing for ref in refs):removed.add(op['op_id']);changed=True
    result=deepcopy(plan);result['operations']=[op for op in operations if op['op_id'] not in removed]
    return result


def minimize(source, destination, *, runtime, image, max_attempts=16, deadline_seconds=180):
    """Bounded deletion reduction, requiring exactly the same observed failure."""
    from app.freecad.contracts import FreeCADOperationPlan
    case,original,_,identity=read_frozen(source)
    image = subprocess.check_output([runtime,'image','inspect','--format','{{.Id}}',image],text=True).strip()
    recorded=json.loads((source/'report.json').read_text())
    if (recorded.get('input') != identity or recorded.get('runtime_image_digest') != image
            or recorded.get('status') != 'failed'
            or not (recorded.get('failed_assertions') or recorded.get('error_code') and recorded.get('failed_op_id'))):
        raise ValueError('source report must identify the same frozen inputs, runtime and reducible failure')
    target=fingerprint(recorded)
    started=time.monotonic();trials=[];budget_exhausted=False
    def attempt(plan):
        nonlocal budget_exhausted
        remaining=deadline_seconds-(time.monotonic()-started)
        used_bytes=sum(path.stat().st_size for path in destination.rglob('*') if path.is_file())
        if len(trials)>=max_attempts or remaining<1 or used_bytes>256*1024*1024:
            budget_exhausted=True;return None
        directory=destination/f'trial-{len(trials):03d}'
        freeze(case,directory,plan)
        report=execute_frozen(directory,runtime=runtime,image=image,timeout=min(120,remaining))
        trials.append({'directory':directory.name,'operations_sha256':sha(plan),'fingerprint':fingerprint(report)})
        return report
    first=attempt(original)
    if first is None or fingerprint(first)!=target:
        raise ValueError('source does not reproduce its recorded native failure')
    best=original
    repeated=attempt(original)
    if repeated is None or fingerprint(repeated)!=target:
        raise ValueError('source failure is not stable across two identical replays')
    for op in original['operations']:
        if op['action']=='document.export' or op['op_id']==first.get('failed_op_id'):continue
        candidate=without_dependents(best,{op['op_id']})
        if candidate==best:continue
        try:FreeCADOperationPlan.model_validate(candidate)
        except ValueError:continue
        result=attempt(candidate)
        if result is None:break
        if fingerprint(result)==target:best=candidate
    confirmations=[]
    for _ in range(2):
        result=attempt(best)
        if result is None:break
        confirmations.append(fingerprint(result)==target)
    final=destination/'reduced';freeze(case,final,best)
    report={'schema_version':'native-oracle-reduction.v1','original_operations_sha256':sha(original),
        'reduced_operations_sha256':sha(best),'original_count':len(original['operations']),
        'reduced_count':len(best['operations']),'fingerprint':target,'trials':trials,
        'confirmed':len(confirmations)==2 and all(confirmations),'budget_exhausted':budget_exhausted,
        'global_minimum_proven':False,'model_calls':0}
    write_json(destination/'reduction.json',report)
    return report


def main():
    root = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=['run','replay','minimize'])
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--source',type=Path)
    parser.add_argument('--seeds',type=int,default=8)
    args = parser.parse_args()
    runtime = os.environ.get('SANDBOX_COMMAND') or os.environ.get('SANDBOX_RUNTIME','podman')
    image = os.environ['SANDBOX_IMAGE']
    args.output.mkdir(parents=True,exist_ok=False)
    image_id = subprocess.check_output([runtime,'image','inspect','--format','{{.Id}}',image],text=True).strip()
    revision = subprocess.check_output(['git','rev-parse','HEAD'],cwd=root,text=True).strip()
    manifest = {'source_commit':revision,'runtime_image':image,'runtime_image_digest':image_id,
        'execution_path':'typed_freecad_runner','model_calls':0,'generator_version':GENERATOR,
        'source_sha256':{str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest() for p in
            [Path(__file__),Path(__file__).with_name('native_oracle_worker.py'),root/'backend/sandbox/freecad_entry.py',
             root/'backend/app/freecad/native_references.py',root/'backend/app/contracts/native_references.py']}}
    write_json(args.output/'manifest.json',manifest)
    image = image_id
    if args.command == 'minimize':
        if args.source is None:parser.error('minimize requires --source')
        report=minimize(args.source,args.output,runtime=runtime,image=image)
        print('CAD_NATIVE_ORACLE_REDUCTION='+json.dumps({k:report[k] for k in ('original_count','reduced_count','confirmed','budget_exhausted')}))
        return 0 if report['confirmed'] else 1
    reports=[]
    if args.command == 'replay':
        if args.source is None:parser.error('replay requires --source')
        case,plan,_,_ = read_frozen(args.source)
        frozen = args.output/case['case_id'];freeze(case,frozen,plan)
        reports.append(execute_frozen(frozen,runtime=runtime,image=image))
    else:
        if args.seeds < 0:parser.error('--seeds must be nonnegative')
        for case in cases(range(args.seeds)):
            frozen = args.output/case['case_id']
            try:
                freeze(case,frozen)
                reports.append(execute_frozen(frozen,runtime=runtime,image=image))
            except (ValueError,TypeError,KeyError) as exc:
                report={'case_id':case['case_id'],'status':'rejected_input','message':str(exc)}
                frozen.mkdir(exist_ok=True);write_json(frozen/'report.json',report);reports.append(report)
    counts={status:sum(r['status']==status for r in reports) for status in sorted({r['status'] for r in reports})}
    summary={'passed':all(r['status']=='passed' for r in reports),
             'planned':len(reports),'counts':counts,'results':reports,**manifest}
    write_json(args.output/'report.json',summary)
    print('CAD_NATIVE_ORACLE='+json.dumps({'planned':len(reports),'counts':counts,'model_calls':0}))
    return 0 if all(r['status']=='passed' for r in reports) else 1


if __name__ == '__main__':
    raise SystemExit(main())
