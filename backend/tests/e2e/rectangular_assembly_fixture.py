"""Source contracts and receiver audit for independently built assembly cases."""
from copy import deepcopy
import json
from pathlib import Path
import re
import sys

from tests.test_rectangular_assembly_requirements import assembly_sources, assembly_proposal
from app.contracts.acceptance_v2 import decode_acceptance
from app.validation.durable_geometry import DurableGeometryReport, verify_geometry_evidence


def fixtures():
    cases=[]
    for scale,parameters in [('original',{'length':90,'width':60,'height':35,'tab_length':12,
        'tab_width':10,'mount_diameter':4.6,'groove_width':2.4,'groove_depth':1.5,'connector_diameter':12.5}),
        ('different',{'length':110,'width':74,'height':46,'tab_length':14,'tab_width':11,
        'mount_diameter':5.2,'groove_width':2.1,'groove_depth':1.2,'connector_diameter':14.5})]:
        text,sources=assembly_sources()
        if scale=='different':
            replacements={'90 mm':'110 mm','60 mm':'74 mm','35 mm':'46 mm','12 mm':'14 mm','10 mm':'11 mm',
                '4.6 mm':'5.2 mm','2.4 mm':'2.1 mm','1.5 mm':'1.2 mm','12.5 mm':'14.5 mm'}
            sources[1]['text']=re.sub('|'.join(map(re.escape,replacements)),lambda m:replacements[m.group()],sources[1]['text'])
            text='\n'.join(s['text'] for s in sources)
        extracted,contract=assembly_proposal(text,sources)
        assert not extracted['unsupported'],extracted['unsupported']
        values={r['scope']['assembly_measurement']:r['comparison']['value'] for r in extracted['requirements']}
        assert [values[k] for k in ['envelope_length','envelope_width','envelope_height']]==[parameters[k] for k in ['length','width','height']]
        for variant in ['good','rotated','wrong_wall','wrong_floor','wrong_lid','wrong_tab_length','wrong_tab_width',
            'wrong_tab_thickness','missing_tab','wrong_mount_diameter','off_center_mount','missing_mount','blind_mount',
            'wrong_groove_width','wrong_groove_depth','broken_groove','off_center_connector','long_side_connector',
            'wrong_connector_diameter','blind_connector','lid_gap','lid_shift','unknown_micro_bore',
            'unknown_nurbs_bore','inner_micro_bore','tab_cavity_channel']:
            cases.append({'name':scale+'-'+variant,'variant':variant,'parameters':parameters,
                          'expected_pass':variant in {'good','rotated'},'contract':contract.model_dump(mode='json')})
    return cases


def verify(path):
    line=next(line for line in Path(path).read_text().splitlines() if line.startswith('CAD_ASSEMBLY_REPORTS='))
    rows=json.loads(line.split('=',1)[1]);mutations=0
    for row in rows:
        report=DurableGeometryReport.model_validate(row['report'])
        kwargs=dict(request_sha256=report.request_sha256,acceptance=decode_acceptance(row['contract']),
                    expected_solid_count=None,measurement_context=row['context'])
        verify_geometry_evidence(report,**kwargs)
        assert (report.outcome=='passed')==row['expected_pass'],row['name']
        for mutation in ['complete','material','faces','measured','tabs','holes','groove','nominal']:
            changed=deepcopy(row['report'])
            evidence=next((e for e in changed['acceptance']['evidence'] if e['outcome']!='indeterminate'),None)
            if evidence is None:continue
            cert=evidence['details']['rectangular_assembly']
            if mutation=='complete':cert['complete']=False
            elif mutation=='material':cert['coverage']['body_missing_mm3']=.00001
            elif mutation=='faces':cert['coverage']['covered_face_indices'].pop()
            elif mutation=='measured':evidence['measured'][0]+=1
            elif mutation=='tabs':cert['tabs'].pop()
            elif mutation=='holes':cert['mounting_holes'].pop()
            elif mutation=='groove':cert['groove']['inner_bounds_mm'][0]+=.1
            elif mutation=='nominal':cert['selection_uses_nominal']=True
            binding=evidence['details']['measurement_binding']
            binding['proof']['selection']['rectangular_assembly']=deepcopy(cert)
            from app.contracts.measurement_binding import binding_digest
            binding['binding_digest']=binding_digest({k:v for k,v in binding.items() if k!='binding_digest'})
            try:verify_geometry_evidence(DurableGeometryReport.model_validate(changed),**kwargs)
            except ValueError:mutations+=1
            else:raise AssertionError('tampered assembly certificate accepted: '+mutation)
    from app.contracts.geometry_request import geometry_request_digest
    preview_line = next(line for line in Path(path).read_text().splitlines() if line.startswith('CAD_ASSEMBLY_PREVIEW_REPORTS='))
    previews = json.loads(preview_line.split('=', 1)[1])
    assert [row['name'] for row in previews] == ['matching', 'native-parts', 'native-fused', 'displaced']
    for row in previews:
        raw = row['report']
        digest = geometry_request_digest(expected_dimensions_mm={}, dimension_tolerance=.05, expected_solid_count=2,
                                         acceptance=row['contract'], measurement_context=row['context'])
        kwargs = dict(request_sha256=digest, acceptance=decode_acceptance(row['contract']),
                      expected_solid_count=2, measurement_context=row['context'])
        verify_geometry_evidence(DurableGeometryReport.model_validate(raw), **kwargs)
        assert (raw['outcome'] == 'passed') == (row['name'] in {'matching', 'native-parts'})
        if row['name'] == 'matching':
            for forged in [None, ['0'*64, raw['artifacts'][1]['sha256']], [raw['artifacts'][0]['sha256'], '0'*64]]:
                altered = deepcopy(raw)
                altered['artifacts'][1]['regenerated_preview'] = forged
                try: verify_geometry_evidence(DurableGeometryReport.model_validate(altered), **kwargs)
                except ValueError: mutations += 1
                else: raise AssertionError('unbound preview accepted')
    print('CAD_ASSEMBLY_RECEIVER='+json.dumps({'cases':len(rows),'preview_cases':len(previews),'rejected_mutations':mutations}))


if __name__=='__main__':
    if sys.argv[1]=='--verify':verify(sys.argv[2])
    else:Path(sys.argv[1]).write_text(json.dumps(fixtures(),ensure_ascii=False))
