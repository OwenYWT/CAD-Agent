"""Preserve bounded chamfer intent, including explicit regions to leave unchanged.

This is a conservative guard for the supported named scopes, not a general
natural-language parser. Contradictions and unsupported set subtraction require
clarification; an exclusion never falls back to selecting every edge.
"""
from __future__ import annotations

from dataclasses import dataclass
import re

_SCOPE = re.compile(
    r'(?P<outer>\ball_outer\b|\b(?:outer|external)\s+edges?\b|外(?:部)?(?:边|棱))'
    r'|(?P<hole_mouths>\bhole[_ ](?:mouths?|rims?|openings?)\b|孔口)'
    r'|(?P<all>\ball\s+edges\b|\bedges\s*=\s*all\b|所有边|全部边)', re.I)
_CLAUSE = re.compile(r'[,，;；。!！?？\n]|(?<!\d)\.(?!\d)|\bbut\b|但是|但|而', re.I)
_PROTECT_BEFORE = re.compile(
    r"\b(?:do\s+not|don['’]t|must\s+not|should\s+not|never|not|no|"
    r"except(?:ing)?|exclud(?:e|es|ing)|without|avoid|preserve)\b"
    r'|不要|不得|禁止|不应|不许|避免|排除|保留|不倒|不处理|不加工', re.I)
_PROTECT_AFTER = re.compile(
    r'\b(?:unchanged|untouched|unmodified|unchamfered|intact|as[- ]is)\b'
    r'|\b(?:must|should|shall)\s+not\b|\b(?:do\s+not|don[’\']t|not)\s+(?:be\s+)?chamfer'
    r'|保持不变|维持原样|保持原样|不变|不处理|不倒角|勿倒角|不要倒角|不得倒角|不作修改', re.I)
_NOT_ONLY = re.compile(
    r"\b(?:not|never|don['’]t)\s+(?:only|just)\b", re.I)
_UNCLEAR_NEGATIVE = re.compile(
    r"\b(?:not|never|don['’]t)\s+(?:exclud\w*|avoid|leave|keep|preserve|unchanged|untouched)\b"
    r'|不能不|不要不|不排除|不保留', re.I)


@dataclass(frozen=True)
class EdgeIntent:
    treated: frozenset[str] = frozenset()
    protected: frozenset[str] = frozenset()

    def scope(self) -> str | None:
        if len(self.treated) > 1 or self.treated & self.protected:
            raise ValueError('ambiguous chamfer scope: confirm outer edges, hole mouths or all edges')
        return next(iter(self.treated), None)


def _intent(text: str) -> EdgeIntent:
    treated, protected = set(), set()
    for clause in _CLAUSE.split(text.lower()):
        mentions = list(_SCOPE.finditer(clause))
        exclusions = []
        bridges = []
        for index, mention in enumerate(mentions):
            start = mentions[index - 1].end() if index else 0
            end = mentions[index + 1].start() if index + 1 < len(mentions) else len(clause)
            before, after = clause[start:mention.start()], clause[mention.end():end]
            # "not only outer edges" adds a scope; it does not protect it.
            before = _NOT_ONLY.sub('', before)
            if _UNCLEAR_NEGATIVE.search(before) or _UNCLEAR_NEGATIVE.search(after):
                raise ValueError('ambiguous chamfer scope: clarify the negated edge instruction')
            excluded = bool(_PROTECT_BEFORE.search(before) or _PROTECT_AFTER.search(after))
            exclusions.append(excluded)
            bridges.append(bool(index and re.fullmatch(r'\s*(?:and|or|和|及|与|、)\s*(?:the\s+)?', before)))
        # Treat coordinated nouns as a group, whether protection precedes or
        # follows them: "avoid A and B" / "leave A and B unchanged".
        for index in range(1, len(mentions)):
            if bridges[index]:
                exclusions[index] |= exclusions[index - 1]
        for index in range(len(mentions) - 2, -1, -1):
            if bridges[index + 1]:
                exclusions[index] |= exclusions[index + 1]
        for mention, excluded in zip(mentions, exclusions):
            (protected if excluded else treated).add(mention.lastgroup)
    intent = EdgeIntent(frozenset(treated), frozenset(protected))
    intent.scope()
    return intent


def requested_edge_scope(text: str) -> str | None:
    intent = _intent(text)
    scope = intent.scope()
    if scope == 'all' and intent.protected:
        raise ValueError('ambiguous chamfer scope: all edges with exclusions needs an explicit supported scope')
    return scope


def _text_values(value):
    """Keep requirement fields separate; JSON punctuation is not user grammar."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _text_values(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _text_values(item)


def validate_chamfer_intent(operation_plan, requirements: dict, objective: str = '') -> None:
    chamfers = [op for op in operation_plan.operations if op.action == 'feature.chamfer']
    if not chamfers:
        return
    declared_intent = _intent('\n'.join(_text_values({key: requirements.get(key) for key in
                        ('description', 'new_features', 'features', 'constraints')})))
    original_intent = _intent(objective)
    declared, original = declared_intent.scope(), original_intent.scope()
    if declared and original and declared != original:
        raise ValueError('planner chamfer scope conflicts with original request')
    expected = original or declared
    protected = original_intent.protected | declared_intent.protected
    if protected and (expected is None or expected == 'all' or expected in protected or 'all' in protected):
        raise ValueError('chamfer scope conflicts with protected edges; confirm an explicit non-overlapping scope')
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
