import pytest
from pydantic import ValidationError
from app.domain.requirement_basis import RequirementBasisV1


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
