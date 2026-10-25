"""Reject impossible measurement instructions before native execution."""
import pytest

from app.contracts.acceptance import AcceptanceContract
from app.validation.acceptance_preflight import validate_new_acceptance


def connectivity(probes, lo=(0, 0, 0), hi=(10, 10, 20)):
    return AcceptanceContract.model_validate({'objective': '顶部开口', 'checks': [{
        'check_id': 'opening', 'kind': 'void_connected', 'description': '顶部开口',
        'source_quote': '顶部开口', 'nominal': 1,
        'scope': {'region_min_mm': lo, 'region_max_mm': hi, 'centers_mm': probes}}]})


@pytest.mark.parametrize('probes', [((5, 5, 10), (5, 5, 20)),
                                    ((5, 5, 10), (5, 5, 21)),
                                    ((5, 5, 10), (5, 5, 10))])
def test_invalid_void_probes_are_corrected_by_planning_not_by_geometry_repair(probes):
    contract = connectivity(probes)
    # Historical contracts remain readable and retain their exact digest.
    digest = contract.digest()
    with pytest.raises(ValueError, match='opening.*probe'):
        validate_new_acceptance(contract)
    assert contract.digest() == digest


def test_valid_interior_probes_preserve_the_frozen_contract():
    contract = connectivity(((5, 5, 10), (5, 5, 19)))
    digest = contract.digest()
    validate_new_acceptance(contract)
    assert contract.digest() == digest


def test_material_thickness_cannot_be_submitted_as_empty_space():
    contract = AcceptanceContract.model_validate({'objective': '壳体壁厚为 3 mm', 'checks': [{
        'check_id': 'wall', 'kind': 'planar_gap', 'description': 'wall',
        'source_quote': '壳体壁厚为 3 mm', 'nominal': 3}]})
    with pytest.raises(ValueError, match='wall.*material'):
        validate_new_acceptance(contract)


def test_an_explicit_air_gap_is_still_a_supported_planar_gap():
    contract = AcceptanceContract.model_validate({'objective': '夹口间隙 3 mm', 'checks': [{
        'check_id': 'gap', 'kind': 'planar_gap', 'description': 'gap',
        'source_quote': '夹口间隙 3 mm', 'nominal': 3}]})
    validate_new_acceptance(contract)


def test_supported_delivery_formats_are_not_unsupported_geometric_requirements():
    contract = connectivity(((5, 5, 10), (5, 5, 19))).model_copy(update={
        'verification_limits': ('导出 STEP 和 STL 文件格式不在 CAD 几何验证范围内。',)})
    with pytest.raises(ValueError, match='artifact delivery'):
        validate_new_acceptance(contract)
    # Specific unimplemented export capabilities and physical performance are
    # not made successful by knowing how to write a basic STEP/STL file.
    for limitation in ('STEP AP242 PMI 注释的保留尚不支持', '防水等级不能由 CAD 几何验证'):
        validate_new_acceptance(contract.model_copy(update={'verification_limits': (limitation,)}))
