"""Acceptance is selected by operation semantics, not only by CAD backend."""
from types import SimpleNamespace

import pytest

from app.workflows.handlers import planning


@pytest.mark.parametrize('backend', ['freecad', 'cadquery', 'auto'])
def test_new_3d_modifications_require_bound_acceptance(backend):
    request = SimpleNamespace(modeling_backend=backend, output_formats=('step', 'stl'))
    sources = [{'text': '孔径6mm', 'source_input_id': 'objective', 'source_kind': 'user'}]
    options = planning.modification_acceptance_options(request, {
        'engineering_acceptance_v1': True, 'acceptance_bindings_v2': True,
        'all_3d_acceptance_v1': True}, sources=sources, base_state=None)
    assert options['require_acceptance'] is True
    assert options['acceptance_version'] == 'engineering-acceptance.v2'
    assert options['requirement_sources'] == sources


def test_historical_cadquery_and_2d_policy_are_not_rewritten():
    request = SimpleNamespace(modeling_backend='cadquery', output_formats=('step',))
    assert planning.modification_acceptance_options(request, {
        'engineering_acceptance_v1': True, 'acceptance_bindings_v2': True}, sources=[], base_state=None) == {}
    request.output_formats = ('dxf', 'svg')
    assert planning.modification_acceptance_options(request, {
        'engineering_acceptance_v1': True, 'acceptance_bindings_v2': True,
        'all_3d_acceptance_v1': True}, sources=[], base_state=None) == {}
