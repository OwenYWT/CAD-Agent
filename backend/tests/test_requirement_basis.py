import pytest
from pydantic import ValidationError
from app.domain.requirement_basis import RequirementBasisV1
from app.models.workflow_requests import McadAgentWorkflowV2Request, OperationContextV1
from app.workflows.handlers.planning import requirements_objective
from app.execution.canonical import canonical_sha256
from uuid import uuid4


def test_missing_dimensions_require_explicit_concept_consent():
    with pytest.raises(ValidationError):RequirementBasisV1(target='iPhone 手机壳')
    basis=RequirementBasisV1(target='iPhone 手机壳',concept_acknowledged=True)
    assert '适配未验证' in basis.planning_context()
    assert '未提供' in basis.planning_context()


def test_reference_label_does_not_prove_dimensions_or_fit():
    with pytest.raises(ValidationError):RequirementBasisV1(target='手机壳',source_kind='reference',source_reference='厂商尺寸图')
    basis=RequirementBasisV1(target='手机壳',source_kind='reference',source_reference='用户提供的尺寸图第 2 页',dimensions='宽度 60.5 mm',fit_notes='相机开孔尚待实测')
    assert '宽度 60.5 mm' in basis.planning_context()
    assert '不是独立校验证据' in basis.planning_context()
    assert '不得擅自替换用户明确尺寸' in basis.planning_context()


def test_client_cannot_claim_independent_fit_validation():
    with pytest.raises(ValidationError):RequirementBasisV1(target='手机壳',concept_acknowledged=True,fit_verified=True)


def test_explicit_design_dimensions_are_not_physical_measurements():
    basis = RequirementBasisV1(target='100×60×3 mm 板', design_scope='geometry',
        source_kind='user_specification', source_reference='用户指定的设计尺寸', dimensions='100×60×3 mm')
    assert '100×60×3 mm' in basis.planning_context()
    assert '适配未验证' not in basis.planning_context()
    assert '不得擅自替换用户明确尺寸' in basis.planning_context()
    assert RequirementBasisV1.model_validate(basis.model_dump()) == basis


def test_design_input_cannot_be_used_as_physical_fit_evidence():
    with pytest.raises(ValidationError):
        RequirementBasisV1(target='手机壳', design_scope='physical_fit', source_kind='user_specification',
            source_reference='随便指定', dimensions='100×60×3 mm')


@pytest.mark.parametrize('operation', ['generate', 'modify'])
def test_revised_basis_changes_planning_input_without_mutating_frozen_request(operation):
    base = uuid4()
    backend = 'auto' if operation == 'generate' else 'freecad'
    request = McadAgentWorkflowV2Request(
        **{field: uuid4() for field in ('workflow_run_id', 'tenant_id', 'project_id', 'principal_id', 'branch_id')},
        expected_base_revision_id=base, operation=operation, modeling_backend=backend,
        objective='按已确认的尺寸建模', operation_context=OperationContextV1(
            rule='explicit_rest_operation', source_channel='rest', requested_operation=operation,
            resolved_operation=operation, submission_modeling_backend=backend, base_revision_id=base,
            **({'base_source_kind':'none'} if operation=='generate' else
               {'base_source_kind':'fcstd_artifact','base_source_id':uuid4(),'base_source_sha256':canonical_sha256({'revision':str(base)})}),
            requirement_basis=RequirementBasisV1(target='板',
                design_scope='geometry', source_kind='user_specification',
                source_reference='用户指定的设计尺寸', dimensions='60×40×9 mm')))
    saved = request.temporal_payload()
    revised = request.temporal_payload()
    revised['operation_context']['requirement_basis']['dimensions'] = '60×40×10 mm'
    updated = McadAgentWorkflowV2Request.model_validate(revised)
    before = requirements_objective(request)
    after = requirements_objective(updated)
    assert '60×40×9 mm' in before and '60×40×10 mm' not in before
    assert '60×40×10 mm' in after and '60×40×9 mm' not in after
    assert '不是实物测量' in after
    assert request.temporal_payload() == saved
    assert canonical_sha256(saved) != canonical_sha256(updated.temporal_payload())


def test_historical_request_without_basis_keeps_exact_objective_and_wire_input():
    request = McadAgentWorkflowV2Request(
        **{field: uuid4() for field in ('workflow_run_id', 'tenant_id', 'project_id', 'principal_id', 'branch_id', 'expected_base_revision_id')},
        operation='generate', objective='既有需求\n保留原文和空白。')
    before = request.temporal_payload()
    assert requirements_objective(request) == request.objective
    assert request.temporal_payload() == before
