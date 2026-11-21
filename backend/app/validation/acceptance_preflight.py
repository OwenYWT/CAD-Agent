"""Validate new measurement instructions without rewriting frozen contracts.

Keep this at the planning boundary: persisted v1 contracts must remain readable
for replay and audit, including the invalid instructions that caused a failure.
"""
import math
import re

from app.contracts.geometry_request import is_whole_world_scope


def validate_v1_source_dimension_axes(
    contract,
    objective: str,
    *,
    verified_state: dict | None = None,
    requirement_sources=None,
) -> None:
    """Keep legacy acceptance dimensions aligned with server-owned semantics.

    v1 contracts predate ``binding_ref`` and therefore cannot use the full v2
    requirement-binding validator.  They still must not reinterpret a named
    width as the pad extrusion (Z) axis.  Validate the deterministic source
    rows that are available and leave unrelated/provider-only checks intact.
    """
    if contract is None or getattr(contract, "schema_version", None) != "engineering-acceptance.v1":
        return
    from app.validation.requirement_binding import extract_source_requirements

    extracted = extract_source_requirements(
        objective,
        verified_state=verified_state,
        requirement_sources=requirement_sources,
    )
    for row in extracted.get("requirements", ()):
        if row.get("kind") != "overall_dimension" or row.get("property") not in {
            "length", "width", "height", "thickness",
        }:
            continue
        candidates = [
            check for check in contract.checks
            if check.kind == "overall_dimension"
            and row.get("source_quote") in check.source_quote
        ]
        if len(candidates) != 1:
            continue
        check = candidates[0]
        expected_axis = tuple(float(value) for value in row["scope"]["axis"])
        actual_axis = tuple(float(value) for value in check.scope.axis or ())
        if actual_axis != expected_axis:
            axis_name = row["property"]
            raise ValueError(
                f"{check.check_id}: overall_dimension axis does not match source "
                f"property {axis_name!r}; expected {list(expected_axis)}, "
                f"got {list(actual_axis)}"
            )
        expected_value = float(row["comparison"]["value"])
        if check.nominal is None or not math.isclose(float(check.nominal), expected_value):
            raise ValueError(
                f"{check.check_id}: overall_dimension nominal does not match "
                f"source property {row['property']!r}"
            )


def validate_modification_dimension_targets(
    modification,
    objective: str,
    *,
    verified_state: dict | None = None,
    requirement_sources=None,
) -> None:
    """Require changed source dimensions to reach the same semantic parameter."""
    if modification is None:
        return
    if not re.search(r"改(?:为|成)|修改|调整|change|set|increase|decrease|make", objective, re.I):
        return
    from app.validation.requirement_binding import extract_source_requirements

    extracted = extract_source_requirements(
        objective,
        verified_state=verified_state,
        requirement_sources=requirement_sources,
    )
    params = {
        re.sub(r"[^a-z0-9\u4e00-\u9fff]", "", str(key).lower()): value
        for key, value in (getattr(modification, "target_params", {}) or {}).items()
    }
    aliases = {
        "length": {"length", "lengthmm", "长", "长度"},
        "width": {"width", "widthmm", "wide", "宽", "宽度"},
        "height": {"height", "heightmm", "high", "高", "高度"},
        "thickness": {"thickness", "thicknessmm", "thick", "厚", "厚度"},
    }
    for row in extracted.get("requirements", ()):
        property_name = row.get("property")
        if row.get("kind") != "overall_dimension" or property_name not in aliases:
            continue
        if not is_whole_world_scope(row['scope']):
            continue
        if row.get("source_input_id", "objective") != "objective":
            continue
        if (row.get("derivation") or {}).get("rule") == "sealed_independent_measurement.v1":
            continue
        alias_candidates = [params[key] for key in aliases[property_name] if key in params]
        canonical = params.get(property_name)
        if canonical is not None and len(alias_candidates) > 1:
            try:
                equivalent = all(
                    math.isclose(float(canonical), float(candidate))
                    for candidate in alias_candidates
                )
            except (TypeError, ValueError):
                equivalent = all(candidate == canonical for candidate in alias_candidates)
            if not equivalent:
                raise ValueError(
                    f"target_params.{property_name}: conflicting aliases specify different values"
                )
            candidates = [canonical]
        else:
            candidates = alias_candidates
        if len(candidates) != 1:
            raise ValueError(
                f"target_params.{property_name}: changed source dimension must use "
                f"the semantic {property_name!r} parameter; expected "
                f"{row['comparison'].get('value')} {row['comparison'].get('unit')}"
            )
        try:
            actual = float(candidates[0])
            expected = float(row["comparison"]["value"])
        except (TypeError, ValueError):
            raise ValueError(f"target_params.{property_name}: value is not numeric")
        if not math.isclose(actual, expected):
            raise ValueError(
                f"target_params.{property_name}: value does not match the saved source dimension"
            )


def validate_new_acceptance(contract) -> None:
    if contract is None:
        return
    for limit in contract.verification_limits:
        # Delivery is validated by execution, artifact readers and candidate
        # sealing. Being outside a *geometry* predicate is not a software
        # inability to deliver STEP/STL. Reject the proposal for replanning;
        # never delete a frozen limit or claim a file was successfully written.
        if (re.search(r'\b(?:STEP|STL|FCStd)\b', limit, re.I)
                and re.search(r'导出|文件|格式|export|file|format', limit, re.I)
                and re.search(r'不在.*几何.*范围|不属于.*几何|outside.*geometr|not.*geometr', limit, re.I)):
            raise ValueError('artifact delivery has its own execution and file validation gates. '
                'Do not list a supported STEP/STL/FCStd format as unavailable solely because '
                'it is outside geometry measurements. Retain the requested outputs in the plan; '
                'a specific unsupported export feature must be described explicitly.')
    for check in contract.checks:
        scope = check.scope
        if check.kind == 'void_connected':
            probes = scope.centers_mm
            lo, hi = scope.region_min_mm, scope.region_max_mm
            if (lo is None or hi is None or any(b <= a for a, b in zip(lo, hi))
                    or any(not all(a < v < b for a, v, b in zip(lo, p, hi)) for p in probes)
                    or any(math.dist(a, b) <= 1e-7 for i, a in enumerate(probes) for b in probes[i+1:])):
                raise ValueError(f'{check.check_id}: connectivity probes must be distinct and strictly '
                    'inside the region, never on its boundary; extend the declared region beyond '
                    'an exterior probe. Correct the measurement instruction before freezing it.')
        if check.kind == 'planar_gap':
            quote = check.source_quote
            context = getattr(contract, 'source_context', None)
            external = context and any(ref.source_quote == quote for ref in context.external_references)
            material = re.search(r'壁厚|板厚|底厚|(?:底板|上盖).*厚|wall\s+thickness|plate\s+thickness', quote, re.I)
            air = re.search(r'间隙|夹口|开口|腔体|空腔|gap|opening|cavity', quote, re.I)
            if material and not air and not external:
                raise ValueError(f'{check.check_id}: planar_gap measures empty space, not material '
                    'thickness. Retain the wall requirement and use a supported wall_thickness '
                    'measurement; do not change the model or downgrade coverage to pass.')
