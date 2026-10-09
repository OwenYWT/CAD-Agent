"""Retained invalid source and a controlled proposal, never a kernel substitute."""
from copy import deepcopy
import json
from pathlib import Path


def failed_plan():
    return json.loads((Path(__file__).parent / 'fixtures/native_failures/touching_profile_plan.json').read_text())


def proposal(plan=None, *, gap=25):
    plan = deepcopy(plan or failed_plan())
    outer, inner = [op['args']['geometry'] for op in plan['operations'] if op['action'] == 'sketch.add_profile']
    x, y = outer['corner']['x'], outer['corner']['y']
    w, h, left, depth = outer['width_mm'], outer['height_mm'], inner['corner']['x'], inner['height_mm']
    points = [(x, y), (left, y), (left, y+depth), (left+gap, y+depth),
              (left+gap, y), (x+w, y), (x+w, y+h), (x, y+h)]
    name = plan['operations'][0]['args']['name']
    operations = []
    def add(action, **args):
        operations.append({'op_id': f'boundary-{len(operations)}', 'action': action, 'args': {'sketch': name, **args}})
    for a, b in zip(points, points[1:]+points[:1]):
        add('sketch.add_geometry', geometry={'kind': 'line', 'start': dict(zip('xy', a)), 'end': dict(zip('xy', b))})
    for i in range(8):
        add('sketch.add_constraint', kind='horizontal' if i % 2 == 0 else 'vertical', first={'geometry_index': i})
        add('sketch.add_constraint', kind='coincident', first={'geometry_index': i, 'point_position': 2},
            second={'geometry_index': (i+1) % 8, 'point_position': 1})
    for i, length in [(0, left-x), (2, gap), (6, w), (1, depth), (7, h)]:
        add('sketch.add_constraint', kind='distance', first={'geometry_index': i}, value_mm=length)
    add('sketch.add_constraint', kind='equal', first={'geometry_index': 1}, second={'geometry_index': 3})
    for kind, value in [('distance_x', x), ('distance_y', y)]:
        add('sketch.add_constraint', kind=kind, first={'geometry_index': 0, 'point_position': 1}, value_mm=value)
    plan['operations'][1:3] = operations
    return plan


def acceptance():
    return {'objective': '设计一个可夹在 25 mm 桌板上的耳机挂钩，最终为一个连通实体', 'checks': [
        {'check_id': 'gap', 'kind': 'surface_clearance', 'nominal': 25.0, 'description': '桌板夹持间隙',
         'source_quote': '25 mm', 'scope': {'centers_mm': [[0, -12.5, -20], [0, 12.5, -20]]}},
        {'check_id': 'one-solid', 'kind': 'solid_count', 'nominal': 1.0,
         'description': '单一连通实体', 'source_quote': '一个连通实体'}]}
