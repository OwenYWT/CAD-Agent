"""Stable identity of the requested measurements (also packaged in workers)."""
import hashlib
import json


def is_whole_world_scope(scope):
    return (scope.get('frame', 'world') == 'world'
            and not any(scope.get(field) for field in ('object_ref', 'component_region', 'region_min_mm',
                        'region_max_mm', 'point_mm', 'centers_mm', 'assembly_measurement')))


def is_body_lid_stl(artifact_format, expected_count, acceptance):
    return (artifact_format == 'stl' and expected_count == 2
            and any(check.get('kind') == 'assembly_predicate'
                    and (check.get('scope') or {}).get('assembly_measurement') == 'body_lid_fit'
                    for check in (acceptance or {}).get('checks', ())))


def is_fused_assembly_preview(artifact_format, expected_count, step_count, acceptance, measurement_context):
    return (expected_count == step_count and (measurement_context or {}).get('modeling_backend') == 'cadquery'
            and is_body_lid_stl(artifact_format, expected_count, acceptance))


def geometry_request_digest(*, expected_dimensions_mm, dimension_tolerance,
                            expected_solid_count=None, acceptance=None, measurement_context=None):
    request = {
        "expected_dimensions_mm": expected_dimensions_mm,
        "dimension_tolerance": dimension_tolerance,
        "expected_solid_count": expected_solid_count,
        "acceptance": acceptance,
    }
    if measurement_context is not None:
        request.update(schema_version='geometry-request.v2', measurement_context=measurement_context)
    return hashlib.sha256(json.dumps(request, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":"), allow_nan=False).encode()).hexdigest()
