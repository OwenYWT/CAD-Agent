"""Quantified predicates over a complete, target-independent hole inventory.

Shared by the isolated geometry worker and the evidence receiver. Feasibility
is a capacitated bipartite matching with lower bounds: every measured passage
belongs to exactly one counted group and satisfies every property of that
group. Overlapping predicates never double-count a hole or hide an extra hole.
"""
from collections import deque
import hashlib
import json
import math

if __package__:
    from .acceptance_comparison import compare_measurement, COMPARISON_POLICY_ID
else:
    from acceptance_comparison import compare_measurement, COMPARISON_POLICY_ID


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
        separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def population_pools(checks):
    pools, group_scopes, groups = {}, {}, {}
    if len({c['check_id'] for c in checks}) != len(checks):
        raise ValueError('duplicate hole population check')
    for check in checks:
        reference = check.get('hole_group') or {}
        if (reference.get('schema_version') != 'hole-group.v1' or not reference.get('group_id')
                or check['kind'] not in {'hole_count', 'hole_diameter', 'hole_depth', 'hole_through'}
                or not check.get('required', True) or check.get('source_kind') == 'assumption'):
            raise ValueError('unsupported hole population predicate or authority')
        group = reference['group_id']
        scope = digest(check.get('scope') or {})
        if group in group_scopes and group_scopes[group] != scope:
            raise ValueError('one hole group cannot use different measurement scopes')
        group_scopes[group] = scope
        groups.setdefault(group, []).append(check)
        pools.setdefault(scope, []).append(check)
    for rows in groups.values():
        if sum(c['kind'] == 'hole_count' for c in rows) != 1 or len(rows) < 2:
            raise ValueError('each hole group needs one count and explicit feature predicates')
    return pools


def quantity_key(check):
    scope = check.get('scope') or {}
    if check['kind'] == 'hole_diameter':
        return 'largest_section_diameter' if scope.get('hole_diameter_mode') == 'largest_section' else 'diameter'
    if check['kind'] == 'hole_depth':
        return 'shaft_length' if scope.get('hole_depth_mode') == 'shaft_length' else 'depth'
    if check['kind'] == 'hole_through':
        return 'through'
    raise ValueError('not a hole property')


def inventory_values(check, inventory):
    """Display the full measurement, never a target-filtered subset."""
    passages = inventory['passages']
    return [len(passages)] if check['kind'] == 'hole_count' else [p[quantity_key(check)] for p in passages]


def verify_hole_coverage(coverage, numerical_tolerance, *, check=None):
    """Recompute whole-boundary ownership as well as the topology certificate.

    This certifies round bores and all through openings, including openings
    without a cylindrical face. Open cavities are not counted as round bores.
    Native measurements, not expected counts/radii, supply every input. Genus
    and volume cannot prove blind-hole absence; no surface may be omitted or
    treated as planar merely because the round-hole detector did not see it.
    """
    if (type(numerical_tolerance) not in {int, float} or not math.isfinite(numerical_tolerance)
            or numerical_tolerance < 0):
        raise ValueError('invalid kernel precision for hole coverage')
    if (not isinstance(coverage, dict) or coverage.get('schema_version') != 'hole-coverage.v2'
            or coverage.get('method') != 'whole_boundary_classification_and_passage_fill'
            or coverage.get('unrepresented_analytic_faces') != []
            or coverage.get('incomplete_passages') != []):
        raise ValueError('hole inventory has unaccounted boundaries or no coverage certificate')

    # Scalar passage properties also apply to enclosed round voids: their
    # diameter is measurable and their exterior-through value is zero. Counts,
    # positions and populations require exterior hole identity, which this
    # detector cannot establish for a part with internal shells. The frozen
    # check, never a worker-supplied exemption, determines this distinction.
    internal_void_properties = bool(check and not check.get('hole_group')
        and check.get('kind') in {'hole_diameter', 'hole_depth', 'hole_through'})

    def genus(topology):
        if not isinstance(topology, list) or not topology:
            raise ValueError('hole coverage lacks whole-solid topology')
        total, face_wires = 0, {}
        for solid in topology:
            shells = solid.get('shells')
            if (not isinstance(shells, list) or not shells
                    or (not internal_void_properties and len(shells) != 1)):
                raise ValueError('hole coverage contains unclassified internal shells')
            for shell in shells:
                vertices, edges, wires = shell.get('vertices'), shell.get('edges'), shell.get('face_wire_counts')
                indices = shell.get('face_indices')
                if (shell.get('closed') is not True or type(vertices) is not int or vertices < 0
                        or type(edges) is not int or edges < 0 or not isinstance(wires, list) or not wires
                        or any(type(w) is not int or w < 1 for w in wires)
                        or not isinstance(indices, list) or len(indices) != len(wires)
                        or any(type(i) is not int or i < 0 for i in indices)
                        or len(set(indices)) != len(indices) or set(indices) & face_wires.keys()):
                    raise ValueError('hole coverage topology is incomplete')
                face_wires.update(zip(indices, wires))
                characteristic = vertices - edges + sum(2 - w for w in wires)
                if characteristic > 2 or characteristic % 2:
                    raise ValueError('hole coverage is not a closed orientable boundary')
                total += (2 - characteristic) // 2
        if sorted(face_wires) != list(range(len(face_wires))):
            raise ValueError('hole coverage lacks a complete original face census')
        return total, face_wires

    source, filled = coverage.get('source_topology'), coverage.get('filled_topology')
    source_genus, source_faces = genus(source)
    filled_genus, _ = genus(filled)
    passages = coverage.get('passages')
    if (not isinstance(passages, list) or any(type(p.get('through')) is not int or p['through'] not in {0, 1}
                                            for p in passages)):
        raise ValueError('hole coverage needs measured exterior connections')
    if len(source) != len(filled) or filled_genus or source_genus != sum(p['through'] for p in passages):
        raise ValueError('unaccounted through openings remain in the whole BRep')
    boundary = coverage.get('boundary_faces')
    if not isinstance(boundary, list) or len(boundary) != len(source_faces):
        raise ValueError('hole coverage lacks a classification for every original face')
    seen, owned = set(), {i: [] for i in range(len(passages))}
    for face in boundary:
        if not isinstance(face, dict):
            raise ValueError('invalid hole boundary classification')
        index, kind, classification = face.get('face_index'), face.get('surface_type'), face.get('classification')
        if (type(index) is not int or index not in source_faces or index in seen
                or type(face.get('wire_count')) is not int or face['wire_count'] != source_faces[index]):
            raise ValueError('hole boundary faces are missing, duplicated or differ from source topology')
        seen.add(index)
        if kind == 'PLANE' and classification == 'planar' and 'passage_index' not in face:
            continue
        orientation = face.get('normal_radial_dot')
        if (kind not in {'CYLINDER', 'CONE'} or type(orientation) not in {int, float}
                or not math.isfinite(orientation)):
            raise ValueError('unclassified curved boundary prevents complete hole coverage')
        if classification == 'outward_round' and orientation > 0 and 'passage_index' not in face:
            continue
        owner = face.get('passage_index')
        if (classification != 'passage_wall' or orientation >= 0 or type(owner) is not int
                or owner not in owned):
            raise ValueError('inward boundary is not owned by a measured passage')
        owned[owner].append(index)
    for index, passage in enumerate(passages):
        indices = passage.get('face_indices')
        if (not isinstance(indices, list) or not indices or any(type(i) is not int for i in indices)
                or sorted(indices) != sorted(owned[index])):
            raise ValueError('measured passages do not exclusively own every inward round face')
    for key in ('source_volume_mm3', 'filled_volume_mm3', 'fill_volume_mm3',
                'material_overlap_mm3', 'surface_area_mm2'):
        value = coverage.get(key)
        if type(value) not in {int, float} or not math.isfinite(value) or value < 0:
            raise ValueError('missing finite hole coverage measurement')
    uncertainty = coverage['surface_area_mm2'] * numerical_tolerance
    if (coverage['source_volume_mm3'] <= 0 or coverage['material_overlap_mm3'] > uncertainty
            or abs(coverage['filled_volume_mm3'] - coverage['source_volume_mm3']
                   - coverage['fill_volume_mm3']) > uncertainty):
        raise ValueError('hole fills overlap material or do not account for the measured added volume')
    return passages


def verify_inventory_coverage(inventory, numerical_tolerance, *, check=None):
    if inventory.get('schema_version') != 'hole-inventory.v2' or inventory.get('complete') is not True:
        raise ValueError('complete measured hole inventory required')
    all_passages = verify_hole_coverage(inventory.get('coverage'), numerical_tolerance, check=check)
    indices = inventory.get('selected_indices')
    if (not isinstance(indices, list) or any(type(i) is not int or not 0 <= i < len(all_passages) for i in indices)
            or len(indices) != len(set(indices))
            or inventory.get('passages') != [all_passages[i] for i in indices]):
        raise ValueError('selected holes differ from the certified whole inventory')


def _count_bounds(comparison, size):
    op = comparison['op']
    lo = comparison.get('value', comparison.get('lower', 0))
    hi = comparison.get('value', comparison.get('upper', size))
    if (op not in {'eq', 'ge', 'le', 'range'} or comparison['unit'] != 'count'
            or any(type(v) not in {int, float} or not math.isfinite(v) or v < 0 or int(v) != v for v in (lo, hi))
            or comparison.get('tolerance_minus', 0) or comparison.get('tolerance_plus', 0)):
        raise ValueError('invalid hole group cardinality')
    return int(lo), min(int(hi), size)


def _partition(eligible, bounds):
    """Integral feasible circulation. No geometry, expected-radius filter or greedy allocation."""
    graph, balance = {}, {}

    def edge(left, right, lower, upper):
        graph.setdefault(left, []); graph.setdefault(right, [])
        forward = [right, upper - lower, len(graph[right])]
        backward = [left, 0, len(graph[left])]
        graph[left].append(forward); graph[right].append(backward)
        balance[left] = balance.get(left, 0) - lower
        balance[right] = balance.get(right, 0) + lower
        return forward

    source, sink, supply, demand = ('source',), ('sink',), ('supply',), ('demand',)
    size = len(eligible)
    if any(lo > hi for lo, hi in bounds.values()) or not sum(lo for lo, _ in bounds.values()) <= size <= sum(hi for _, hi in bounds.values()):
        return None
    memberships = {}
    for index, choices in enumerate(eligible):
        hole = ('hole', index)
        edge(source, hole, 1, 1)
        for group in choices:
            memberships[index, group] = edge(hole, ('group', group), 0, 1)
    for group, (lo, hi) in bounds.items():
        edge(('group', group), sink, lo, hi)
    edge(sink, source, 0, size)
    required = 0
    for node, amount in list(balance.items()):
        if amount > 0:
            edge(supply, node, 0, amount); required += amount
        elif amount < 0:
            edge(node, demand, 0, -amount)
    delivered = 0
    while delivered < required:
        parents, queue = {supply: None}, deque([supply])
        while queue and demand not in parents:
            node = queue.popleft()
            for item in graph.get(node, []):
                target, capacity, _ = item
                if capacity > 0 and target not in parents:
                    parents[target] = (node, item); queue.append(target)
        if demand not in parents:
            return None
        amount, node = required - delivered, demand
        while node != supply:
            node, item = parents[node]; amount = min(amount, item[1])
        node = demand
        while node != supply:
            previous, item = parents[node]
            item[1] -= amount; graph[node][item[2]][1] += amount; node = previous
        delivered += amount
    assignment = {group: [] for group in bounds}
    for (index, group), item in memberships.items():
        if item[1] == 0:
            assignment[group].append(index)
    if sorted(i for values in assignment.values() for i in values) != list(range(size)):
        raise ValueError('incomplete population partition')
    return assignment


def compare_hole_populations(checks, inventory, numerical_tolerance, *, policy_id=COMPARISON_POLICY_ID):
    pools = population_pools(checks)
    if len(pools) != 1:
        raise ValueError('population comparison needs one independently selected scope')
    # Validate precision even when there are zero measured features.
    compare_measurement({'op': 'eq', 'value': 0, 'unit': 'mm'}, [], numerical_tolerance, policy_id=policy_id)
    verify_inventory_coverage(inventory, numerical_tolerance)
    groups = {}
    for check in sorted(checks, key=lambda c: c['check_id']):
        groups.setdefault(check['hole_group']['group_id'], []).append(check)
    groups = dict(sorted(groups.items()))
    keys = {quantity_key(c) for c in checks if c['kind'] != 'hole_count'}
    passages = inventory['passages']
    for passage in passages:
        for key in keys:
            value = passage.get(key)
            if (type(value) not in {int, float} or not math.isfinite(value) or value < 0
                    or (key == 'through' and value not in {0, 1})):
                raise ValueError('missing or invalid independently measured hole property')
    bounds = {group: _count_bounds(next(c['comparison'] for c in rows if c['kind'] == 'hole_count'), len(passages))
              for group, rows in groups.items()}
    eligible = [[group for group, rows in groups.items() if all(
        compare_measurement(c['comparison'], [passage[quantity_key(c)]], numerical_tolerance,
                            policy_id=policy_id)['outcome'] == 'passed'
        for c in rows if c['kind'] != 'hole_count')] for passage in passages]
    assignment = _partition(eligible, bounds)
    return {'schema_version': 'hole-population-evaluation.v1',
        'outcome': 'passed' if assignment is not None else 'failed',
        'comparison_policy_id': policy_id, 'numerical_tolerance': numerical_tolerance,
        'checks_sha256': digest(sorted(checks, key=lambda c: c['check_id'])),
        'inventory_sha256': digest(inventory), 'feature_count': len(passages),
        'all_inventory_required': True, 'group_bounds': {k: list(v) for k, v in bounds.items()},
        'eligible_groups': eligible, 'assignment': assignment}
