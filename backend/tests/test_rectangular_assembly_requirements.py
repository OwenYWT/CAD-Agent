"""Independently specified assembly obligations, including confirmed scope."""
from copy import deepcopy
import json
import re
from pathlib import Path

import pytest

from app.contracts.acceptance_v2 import AcceptanceContractV2, RequirementBindingError
from app.validation.requirement_binding import (
    bind_acceptance_proposal, extract_source_requirements, validate_requirement_bindings,
)


ENVELOPE_CONFIRMATION = '总体尺寸包含装配后的盒体和上盖，不包含安装耳'


def assembly_sources(*, dimensions=None, confirmation=ENVELOPE_CONFIRMATION, scope='concept_geometry'):
    case = json.loads((Path(__file__).parent/'fixtures/intake_1030_feedback.json').read_text())['cases'][0]
    sources = [
        {'source_input_id':'objective', 'source_kind':'user', 'text':case['objective'], 'execution_scope':scope},
        {'source_input_id':'dimensions', 'source_kind':'user', 'text':dimensions or case['dimensions'], 'execution_scope':scope},
    ]
    if confirmation:
        sources.append({'source_input_id':'envelope-confirmation', 'source_kind':'confirmed',
                        'text':confirmation, 'execution_scope':scope})
    return '\n'.join(s['text'] for s in sources), sources


def assembly_proposal(text, sources):
    extracted = extract_source_requirements(text, requirement_sources=sources)
    proposal = {'objective':text, 'checks':[
        {'check_id':str(i), 'kind':r['kind'], 'description':r['property'],
         'binding_ref':r['requirement_id'], 'comparison':r['comparison'], 'scope':r['scope'],
         'source_quote':r['source_quote'], 'source_kind':r['source_kind']}
        for i,r in enumerate(extracted['requirements'])]}
    bind_acceptance_proposal(text, proposal, requirement_sources=sources)
    return extracted, AcceptanceContractV2.model_validate(proposal)


def test_confirmed_full_assembly_retains_every_quantity_and_relationship():
    text, sources = assembly_sources()
    extracted, contract = assembly_proposal(text, sources)
    assert not extracted['unsupported'], extracted['unsupported']
    actual = {(r['scope'].get('assembly_measurement'),r['comparison']['value']) for r in extracted['requirements']}
    expected = {
        ('envelope_length',90.),('envelope_width',60.),('envelope_height',35.),
        ('side_wall_thickness',3.),('floor_thickness',3.),('lid_thickness',3.),
        ('tab_length',12.),('tab_width',10.),('tab_thickness',3.),('tab_count',4.),
        ('mounting_hole_diameter',4.6),('mounting_hole_count',4.),
        ('groove_width',2.4),('groove_depth',1.5),('connector_diameter',12.5),
        ('body_lid_fit',1.),('tabs_at_corners',1.),('mounting_holes_on_tabs',1.),
        ('mounting_holes_centered',1.),('mounting_holes_isolated',1.),
        ('groove_at_interface',1.),('connector_through',1.),('connector_short_side_centered',1.),
        ('envelope_excludes_tabs',1.),
    }
    assert actual == expected
    assert validate_requirement_bindings(text, contract, requirement_sources=sources)['status']=='bound'
    assert {n.category for n in contract.verification_notices} == {'water_protection','manufacturing_process'}


@pytest.mark.parametrize('measurement',[
    'envelope_height','side_wall_thickness','lid_thickness','tab_count','tab_width',
    'mounting_holes_centered','mounting_holes_isolated','groove_width','groove_depth',
    'connector_short_side_centered','body_lid_fit',
])
def test_assembly_contract_cannot_omit_a_component_or_relationship(measurement):
    text,sources=assembly_sources()
    extracted,contract=assembly_proposal(text,sources)
    assert not extracted['unsupported'], extracted['unsupported']
    proposal=contract.model_dump(mode='json')
    proposal['checks']=[c for c in proposal['checks'] if c['scope'].get('assembly_measurement')!=measurement]
    with pytest.raises(RequirementBindingError,match='coverage'):
        validate_requirement_bindings(text,AcceptanceContractV2.model_validate(proposal),requirement_sources=sources)


def test_assembly_dimensions_are_parameterized_and_keep_source_authority():
    text,sources=assembly_sources()
    replacements={'90 mm':'110 mm','60 mm':'74 mm','35 mm':'46 mm','12 mm':'14 mm',
                  '10 mm':'11 mm','4.6 mm':'5.2 mm','2.4 mm':'2.1 mm','1.5 mm':'1.2 mm','12.5 mm':'14.5 mm'}
    sources[1]['text']=re.sub('|'.join(map(re.escape,replacements)),lambda m:replacements[m.group()],sources[1]['text'])
    text='\n'.join(s['text'] for s in sources)
    extracted,contract=assembly_proposal(text,sources)
    assert not extracted['unsupported'], extracted['unsupported']
    values={r['scope']['assembly_measurement']:r['comparison']['value'] for r in extracted['requirements']}
    assert [values[k] for k in ['envelope_length','envelope_width','envelope_height','tab_length','groove_width']]==[110,74,46,14,2.1]
    forged=deepcopy(contract.model_dump(mode='json'))
    next(c for c in forged['checks'] if c['scope']['assembly_measurement']=='tab_width')['scope']['assembly_measurement']='envelope_width'
    with pytest.raises(RequirementBindingError,match='scope'):
        validate_requirement_bindings(text,AcceptanceContractV2.model_validate(forged),requirement_sources=sources)


@pytest.mark.parametrize('confirmation',[None,'总体尺寸包含安装耳和上盖',
    ENVELOPE_CONFIRMATION+'；总体尺寸包含安装耳'])
def test_unconfirmed_or_conflicting_envelope_scope_never_becomes_an_assumption(confirmation):
    text,sources=assembly_sources(confirmation=confirmation)
    result=extract_source_requirements(text,requirement_sources=sources)
    assert result['unsupported']


def test_concept_geometry_confirmation_does_not_certify_physical_waterproofing():
    text,sources=assembly_sources(scope='cad_geometry')
    result=extract_source_requirements(text,requirement_sources=sources)
    assert any(x['reason']=='physical_performance_not_verified' for x in result['unsupported'])


@pytest.mark.parametrize('extra',['密封槽沿复杂曲线变化','安装孔偏离安装耳中心2mm',
    '上盖与盒体间隙0.2mm','另有两个隔舱','安装耳厚度不少于4mm'])
def test_unimplemented_or_conflicting_assembly_conditions_remain_visible(extra):
    text,sources=assembly_sources()
    sources[1]['text']+='\n'+extra
    result=extract_source_requirements('\n'.join(s['text'] for s in sources),requirement_sources=sources)
    assert result['unsupported'], result
