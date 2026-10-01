"""Stable identity of the requested measurements (also packaged in workers)."""
import hashlib
import json


def geometry_request_digest(*, expected_dimensions_mm, dimension_tolerance,
                            expected_solid_count=None, acceptance=None):
    request = {
        "expected_dimensions_mm": expected_dimensions_mm,
        "dimension_tolerance": dimension_tolerance,
        "expected_solid_count": expected_solid_count,
        "acceptance": acceptance,
    }
    return hashlib.sha256(json.dumps(request, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":"), allow_nan=False).encode()).hexdigest()
