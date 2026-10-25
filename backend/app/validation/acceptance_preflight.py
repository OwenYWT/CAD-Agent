"""Validate new measurement instructions without rewriting frozen contracts.

Keep this at the planning boundary: persisted v1 contracts must remain readable
for replay and audit, including the invalid instructions that caused a failure.
"""
import math
import re


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
            material = re.search(r'壁厚|板厚|底厚|(?:底板|上盖).*厚|wall\s+thickness|plate\s+thickness', quote, re.I)
            air = re.search(r'间隙|夹口|开口|腔体|空腔|gap|opening|cavity', quote, re.I)
            if material and not air:
                raise ValueError(f'{check.check_id}: planar_gap measures empty space, not material '
                    'thickness. Retain the wall requirement and use a supported wall_thickness '
                    'measurement; do not change the model or downgrade coverage to pass.')
