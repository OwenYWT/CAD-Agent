"""Real incorrect models must fail the independent oracle; shrink the same cut failure."""
import json
import os
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from benchmark.native_oracle import cases, compile_plan, execute_frozen, freeze, minimize, read_frozen, write_json

root=Path(sys.argv[1]);root.mkdir(parents=True,exist_ok=False)
runtime=os.environ.get('SANDBOX_COMMAND') or os.environ.get('SANDBOX_RUNTIME','podman')
image=os.environ['SANDBOX_IMAGE']
reports=[]
for kind in ('blind-for-through','missing-hole','displaced-hole'):
    case=cases()[0];case['case_id']=kind
    plan=compile_plan(case)
    cut=next(op for op in plan['operations'] if op['action']=='feature.hole')
    if kind=='blind-for-through':
        cut['args'].pop('through_all');cut['args']['depth_mm']=6
    elif kind=='missing-hole':
        plan['operations']=[op for op in plan['operations'] if not op['op_id'].startswith('hole-0')]
    else:
        profile=next(op for op in plan['operations'] if op['op_id']=='hole-0-profile')
        profile['args']['geometry']['center']['x']+=2
    directory=root/kind;freeze(case,directory,plan)
    report=execute_frozen(directory,runtime=runtime,image=image)
    assert report['status']=='failed' and report['failed_assertions'],report
    assert all(report['measurements'][name]['valid'] for name in ('fcstd','step'))
    if kind=='blind-for-through':assert 'fcstd.hole-0.through' in report['failed_assertions']
    if kind=='displaced-hole':assert 'fcstd.hole-0.identity' in report['failed_assertions']
    reports.append({'case_id':kind,'expected':'failed','actual':report['status'],
                    'failed_assertions':report['failed_assertions']})

case=next(case for case in cases() if case['case_id']=='two-holes')
case['case_id']='wrong-direction';plan=compile_plan(case)
next(op for op in plan['operations'] if op['op_id']=='hole-1-cut')['args']['reversed']=True
source=root/'wrong-direction';freeze(case,source,plan)
original=execute_frozen(source,runtime=runtime,image=image)
assert original['status']=='failed' and original['error_code']=='subtractive_feature_no_effect',original
reduced=root/'reduction';reduced.mkdir()
reduction=minimize(source,reduced,runtime=runtime,image=image,max_attempts=16)
assert reduction['confirmed'] and reduction['reduced_count']<reduction['original_count'],reduction
assert reduction['fingerprint']['failed_op_id']=='hole-1-cut'
assert reduction['fingerprint']['diagnosis']=='wrong_direction',reduction
assert read_frozen(source)[1]==plan
# A new failure or runtime must not be relabelled as a reduction of the saved one.
for changed in ('failure','image'):
    mismatched=root/('mismatched-'+changed);freeze(case,mismatched,plan)
    claimed=dict(original)
    if changed=='failure':claimed['diagnosis']='unrelated_failure'
    else:claimed['runtime_image_digest']='0'*64
    write_json(mismatched/'report.json',claimed)
    try:minimize(mismatched,root/('rejected-'+changed),runtime=runtime,image=image)
    except ValueError as error:
        assert ('recorded native failure' if changed=='failure' else 'same frozen inputs, runtime') in str(error),str(error)
    else:raise AssertionError('changed original '+changed+' was accepted for reduction')
write_json(root/'report.json',{'passed':True,'controls':reports,'reduction':reduction,
    'changed_original_failure_rejected':True,'changed_image_rejected':True,'model_calls':0})
print('CAD_NATIVE_ORACLE_CONTROLS='+json.dumps({'incorrect_models_rejected':len(reports),
    'same_failure_reduction_confirmed':True,'original_operations':reduction['original_count'],
    'reduced_operations':reduction['reduced_count'],'changed_original_failure_rejected':True,
    'changed_image_rejected':True,'model_calls':0}))
