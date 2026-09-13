from copy import deepcopy
from uuid import uuid4
import pytest
from app.execution.canonical import canonical_sha256
from app.services.task_evidence import project_evidence


def evidence():
    report = {'schema_version':'durable-geometry-report.v1', 'expected_dimensions_mm':{'length':60},
        'dimension_tolerance':0.01,'artifacts':[], 'runtime_provenance':{'internal_path':'hidden'}}
    return {'id':uuid4(),'workflow_run_id':uuid4(),'staging_manifest_id':uuid4(),'evidence':report,
        'evidence_hash':canonical_sha256(report),'gate':'geometry','mode':'required','outcome':'passed'}


def test_only_selected_sealed_evidence_can_claim_a_revision():
    row=evidence();revision={'id':uuid4(),'manifest':{
        'selected_manifests':[{'staging_manifest_id':str(row['staging_manifest_id'])}],
        'validation':{'gates':[{'evidence_id':str(row['id']),'evidence_hash':row['evidence_hash']}]}}}
    projected=project_evidence(row,revision)
    assert projected['selected_for_revision'] is True and projected['revision_id']==str(revision['id'])
    assert projected['report']['expected_dimensions_mm']=={'length':60}
    assert 'runtime_provenance' not in projected['report']
    older=deepcopy(row);older['staging_manifest_id']=uuid4()
    assert project_evidence(older,revision)['revision_id'] is None
    wrong_hash=deepcopy(revision);wrong_hash['manifest']['validation']['gates'][0]['evidence_hash']='0'*64
    assert project_evidence(row,wrong_hash)['selected_for_revision'] is False


def test_unsealed_and_corrupt_checks_cannot_claim_saved_model_validation():
    row=evidence()
    assert project_evidence(row,None)['revision_id'] is None
    row['evidence']['expected_dimensions_mm']['length']=99
    with pytest.raises(ValueError,match='完整性'):project_evidence(row,None)
