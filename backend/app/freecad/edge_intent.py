"""Preserve bounded chamfer scope across compilation, generation and repair."""
from __future__ import annotations

import json
import re


def requested_edge_scope(text: str) -> str | None:
    normalized = text.lower()
    scopes = set()
    if re.search(r'\ball_outer\b|\b(?:outer|external)\s+edges?\b|外(?:部)?(?:边|棱)', normalized):
        scopes.add('outer')
    if re.search(r'\bhole[_ ](?:mouths?|rims?)\b|孔口', normalized):
        scopes.add('hole_mouths')
    if re.search(r'\ball\s+edges\b|\bedges\s*=\s*all\b|所有边|全部边', normalized):
        scopes.add('all')
    if len(scopes) > 1:
        raise ValueError('ambiguous chamfer scope: confirm outer edges, hole mouths or all edges')
    return next(iter(scopes), None)


def validate_chamfer_intent(operation_plan, requirements: dict, objective: str = '') -> None:
    chamfers = [op for op in operation_plan.operations if op.action == 'feature.chamfer']
    if not chamfers:
        return
    text = json.dumps({key:requirements.get(key) for key in
                       ('description','new_features','features','constraints')},ensure_ascii=False)
    declared = requested_edge_scope(text)
    original = requested_edge_scope(objective)
    if declared and original and declared != original:
        raise ValueError('planner chamfer scope conflicts with original request')
    expected = original or declared
    for operation in chamfers:
        args = operation.typed_args()
        if expected is None:
            if args.selector is None:
                raise ValueError('chamfer scope is missing: confirm edges or select explicit topology')
        elif args.edge_scope != expected:
            raise ValueError(f'chamfer must preserve requested edge_scope={expected}')


def validate_chamfer_repair(before, after) -> None:
    def treatments(plan):
        return {op.args['name']:op.args for op in plan.operations if op.action == 'feature.chamfer'}
    if treatments(before) != treatments(after):
        raise ValueError('repair cannot add, remove or change chamfer target, size or edge scope')
