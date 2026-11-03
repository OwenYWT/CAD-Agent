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
    print('CAD_ASSEMBLY_RECEIVER='+json.dumps({'cases':len(rows),'rejected_mutations':mutations}))


if __name__=='__main__':
    if sys.argv[1]=='--verify':verify(sys.argv[2])
    else:Path(sys.argv[1]).write_text(json.dumps(fixtures(),ensure_ascii=False))
