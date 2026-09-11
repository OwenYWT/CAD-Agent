"""Analytic DFM acceptance inside the real pinned CAD sandbox, no test doubles.

Mount backend/sandbox on /testing, this script on /tests and run with
PYTHONPATH=/testing using the sandbox's Python interpreter.
"""
import hashlib
import json
from pathlib import Path
import tempfile

import cadquery as cq
from dfm_validation import evaluate_dfm


def check(shape,rules):
    with tempfile.TemporaryDirectory() as folder:
        path=Path(folder); model=path/'model.step'; policy=path/'policy.json'
        cq.exporters.export(shape,str(model))
        data={"schema_version":"dfm-policy-snapshot.v1","process":"FDM","material":"PLA",
            "knowledge_constraints":{},"rules":rules}
        raw=json.dumps(data,sort_keys=True,separators=(',',':')).encode();policy.write_bytes(raw)
        return evaluate_dfm(model,policy,expected_policy_hash=hashlib.sha256(raw).hexdigest())


def rule(name,category,**thresholds):
    return {"id":name,"category":category,"check_type":"geometric","severity":"warning",**thresholds}


def bridge(gap):
    left=cq.Workplane('XY').box(4,10,10,centered=False)
    right=cq.Workplane('XY').box(4,10,10,centered=False).translate((gap+4,0,0))
    roof=cq.Workplane('XY').box(gap+8,10,2,centered=False).translate((0,0,10))
    return left.union(right).union(roof)


def main():
    box=cq.Workplane('XY').box(20,10,4,centered=False)
    r=check(box,[rule('fdm_overhang','overhang',threshold_max=0.2),rule('fdm_min_feature','feature',threshold_min=.4),rule('fdm_bridge_distance','feature',threshold_max=0)])
    assert r['outcome']=='passed',r
    assert r['metrics']['overhang_ratio']==0 and r['metrics']['min_feature_size_mm']==4
    records={'bed_contact':r}
    for thickness in (.795, .8, .805):
        coupon=cq.Workplane('XY').box(20,10,thickness,centered=False)
        measured=check(coupon,[rule('fdm_wall_thickness','wall_thickness',threshold_min=.8)])
        assert abs(measured['metrics']['min_wall_thickness_mm']-thickness)<1e-5,measured
        assert measured['outcome']==('failed' if thickness<.8 else 'passed'),measured
        records[f'wall_boundary_{thickness}']=measured
    for gap,angle in ((10,0),(20,0),(10,37)):
        shape=bridge(gap).rotate((0,0,0),(0,0,1),angle)
        r=check(shape,[rule('fdm_bridge_distance','feature',threshold_max=15)])
        assert abs(r['metrics']['bridge_span_mm']-gap)<.01,r
        assert r['outcome']==('failed' if gap>15 else 'passed'),r
        records[f'bridge_{gap}_{angle}']=r
    small=box.union(cq.Workplane('XY').box(.3,4,2,centered=False).translate((4,3,4)))
    r=check(small,[rule('fdm_min_feature','feature',threshold_min=.4)])
    assert r['outcome']=='failed' and abs(r['metrics']['min_feature_size_mm']-.3)<.001,r
    records['small_rib']=r
    cantilever=cq.Workplane('XY').box(4,10,10,centered=False).union(cq.Workplane('XY').box(20,10,2,centered=False).translate((0,0,10)))
    r=check(cantilever,[rule('fdm_bridge_distance','feature',threshold_max=15)])
    assert r['outcome']=='indeterminate' and r['metrics']['unanchored_roof_area_mm2']>0,r
    records['cantilever']=r
    print('CAD_DFM_ACCEPTANCE='+json.dumps(records))


main()
