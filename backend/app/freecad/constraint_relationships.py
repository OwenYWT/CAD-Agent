"""Conservative, exact proofs for supported sketch relations.

This does not replace Sketcher. It proves linear implications between constraint
expressions, then the real kernel must solve and independently validate the result.
Unsupported geometry/relations return unknown; they never become editable by default.
"""
from __future__ import annotations

from fractions import Fraction
import math

TOLERANCE = 1e-7  # Native length comparison, mm; never a replacement design dimension.


def number(value):
    if isinstance(value, bool) or not math.isfinite(float(value)):
        raise ValueError('constraint relationship requires finite numbers')
    return Fraction(str(value))


def _point(index, position, geometry):
    obj = geometry[index]
    field = {1: 's', 2: 'e', 3: 'c'}.get(position)
    key = {'s': 'start', 'e': 'end', 'c': 'center'}.get(field)
    if not key or key not in obj:
        raise ValueError('constraint point is not present in the construction basis')
    return tuple(f'{index}:{field}{axis}' for axis in 'xy')


def _length(index, geometry):
    obj = geometry[index]
    if 'Line' not in obj.get('type', '') or 'start' not in obj or 'end' not in obj:
        return None
    delta = [obj['end'][i] - obj['start'][i] for i in (0, 1)]
    axis = next((i for i in (0, 1) if abs(delta[1-i]) <= TOLERANCE and abs(delta[i]) > TOLERANCE), None)
    if axis is None:
        return None
    sign = 1 if delta[axis] > 0 else -1
    coordinate = 'xy'[axis]
    return {f'{index}:e{coordinate}': Fraction(sign), f'{index}:s{coordinate}': Fraction(-sign)}


def _subtract(first, second):
    result = dict(first)
    for key, coefficient in second.items():
        result[key] = result.get(key, 0) - coefficient
    return {k: v for k, v in result.items() if v}


def relation_rows(args, geometry):
    """Return equations ``terms · coordinates = value``, or unknown (None)."""
    by_index = {g['index']: g for g in geometry}
    kind = args['kind']
    first = args['first']['geometry_index']
    second = (args.get('second') or {}).get('geometry_index')
    if first not in by_index or second is not None and second not in by_index:
        return None  # External geometry and axes require native proofs, not invented coordinates.
    def row(terms, value=0):
        return {'terms': {k: number(v) for k, v in terms.items() if v}, 'value': number(value)}
    try:
        if kind in {'horizontal', 'vertical'}:
            coordinate = 'y' if kind == 'horizontal' else 'x'
            if _length(first, by_index) is None:
                return None
            return [row({f'{first}:e{coordinate}': 1, f'{first}:s{coordinate}': -1})]
        if kind in {'radius', 'diameter'}:
            if 'radius_mm' not in by_index[first] or 'Circle' not in by_index[first].get('type', ''):
                return None
            return [row({f'{first}:r': 1 if kind == 'radius' else 2}, args['value_mm'])]
        if kind == 'distance':
            terms = _length(first, by_index)
            return [row(terms, args['value_mm'])] if terms else None
        if kind in {'distance_x', 'distance_y'}:
            point = _point(first, args['first']['point_position'], by_index)
            if second is not None:
                return None
            return [row({point[0 if kind == 'distance_x' else 1]: 1}, args['value_mm'])]
        if kind == 'coincident':
            a = _point(first, args['first']['point_position'], by_index)
            if args['second'] == {'datum': 'origin'}:
                return [row({coordinate: 1}) for coordinate in a]
            b = _point(second, args['second']['point_position'], by_index)
            return [row(_subtract({left: Fraction(1)}, {right: Fraction(1)})) for left, right in zip(a, b)]
        if kind == 'equal':
            a, b = by_index[first], by_index[second]
            if all('Circle' in o.get('type', '') and 'radius_mm' in o for o in (a, b)):
                return [row({f'{first}:r': 1, f'{second}:r': -1})]
            left, right = _length(first, by_index), _length(second, by_index)
            return [row(_subtract(left, right))] if left and right else None
    except (KeyError, TypeError, ValueError):
        return None
    return None


def native_args(constraint):
    kinds = {'Horizontal': 'horizontal', 'Vertical': 'vertical', 'Distance': 'distance',
        'DistanceX': 'distance_x', 'DistanceY': 'distance_y', 'Radius': 'radius',
        'Diameter': 'diameter', 'Coincident': 'coincident', 'Equal': 'equal'}
    kind = kinds.get(constraint['type'])
    if kind is None:
        return None
    first = {'geometry_index': constraint['first'],
             'point_position': constraint.get('first_position') or None}
    return {'kind': kind, 'first': first,
        'second': ({'datum': 'origin'} if kind == 'coincident' and constraint['second'] == -1 and constraint.get('second_position') == 1
                   else {'geometry_index': constraint['second'], 'point_position': constraint.get('second_position') or None})
                  if kind in {'equal', 'coincident'} else None,
        'value_mm': constraint['value'] if kind in {'distance', 'distance_x', 'distance_y', 'radius', 'diameter'} else None}


def coordinates(geometry):
    values = {}
    for obj in geometry:
        for key, prefix in (('start', 's'), ('end', 'e'), ('center', 'c')):
            if key in obj:
                for i, axis in enumerate('xy'):
                    values[f"{obj['index']}:{prefix}{axis}"] = number(obj[key][i])
        if 'radius_mm' in obj:
            values[f"{obj['index']}:r"] = number(obj['radius_mm'])
    return values


def measured(row, geometry):
    values = coordinates(geometry)
    return sum(coefficient * values[key] for key, coefficient in row['terms'].items())


def satisfies(row, geometry):
    return abs(float(measured(row, geometry) - row['value'])) <= TOLERANCE


def derive_row(target, rows):
    """Construct an exact implication certificate, including the nominal constants.

    Coefficients reference the retained equations. They can be re-evaluated after
    a parameter change; merely matching today's shape is insufficient.
    """
    def vector(row):
        return {**row['terms'], **({'~constant': -row['value']} if row['value'] else {})}
    echelon = {}
    for index, row in enumerate(rows):
        v, coefficients = vector(row), {index: Fraction(1)}
        for pivot in sorted(echelon):
            if pivot in v:
                existing, weights = echelon[pivot]
                factor = v[pivot]
                v = _subtract(v, {k: factor * value for k, value in existing.items()})
                coefficients = _subtract(coefficients, {k: factor * value for k, value in weights.items()})
        if v:
            pivot = min(v)
            scale = v[pivot]
            echelon[pivot] = ({k: value / scale for k, value in v.items()},
                             {k: value / scale for k, value in coefficients.items()})
    v, coefficients = vector(target), {}
    for pivot in sorted(echelon):
        if pivot in v:
            existing, weights = echelon[pivot]
            factor = v[pivot]
            v = _subtract(v, {k: factor * value for k, value in existing.items()})
            for key, value in weights.items():
                coefficients[key] = coefficients.get(key, 0) + factor * value
    if v:
        return None
    return [coefficients.get(index, Fraction(0)) for index in range(len(rows))]


def serialize_row(row):
    return {'terms': {key: str(value) for key, value in row['terms'].items()}, 'value': str(row['value'])}


def deserialize_row(row):
    return {'terms': {key: Fraction(value) for key, value in row['terms'].items()}, 'value': Fraction(row['value'])}
