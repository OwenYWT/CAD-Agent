"""Execution support does not authorize unrestricted repair mutations."""
from dataclasses import dataclass, asdict

from app.freecad.contracts import FreeCADPlanningError


@dataclass(frozen=True)
class RepairCapability:
    executable: bool
    diagnosable: bool
    repairable: bool
    allowed_fields: tuple[str, ...] = ()
    required_evidence: tuple[str, ...] = ('failed_operation',)
    block_reason: str | None = None


def resolve_repair_capability(action, failure, verified_context) -> RepairCapability:
    executable = action in {'feature.pocket', 'feature.hole', 'feature.loft', 'feature.sweep', 'feature.revolve'}
    if action not in {'feature.pocket', 'feature.hole'}:
        return RepairCapability(executable, executable, False, block_reason='unsupported_repair')
    failed = verified_context.get('failed_operation')
    if not failed or failed.get('op_id') != failure.get('operation_id') or failed.get('action') != action:
        return RepairCapability(True, True, False, block_reason='missing_diagnostic')
    fields = [] if (failure.get('details') or {}).get('direction_locked') else ['reversed']
    if verified_context.get('private_profile') and verified_context.get('frozen_acceptance'):
        fields.append('private_profile_placement')
    return RepairCapability(True, True, bool(fields), tuple(fields),
        block_reason=None if fields else 'scope_protected')


def require_cut_repair_capability(source, failure):
    if failure.get('error_code') != 'subtractive_feature_no_effect':
        return None
    failed = next((op for op in source.operations if op.op_id == failure.get('operation_id')), None)
    profile = failed.args.get('profile') if failed else None
    private = profile and any(op.action == 'sketch.create' and op.args['name'] == profile for op in source.operations)
    private = private and not any(op != failed and (op.args.get('profile') == profile
        or op.args.get('path') == profile or profile in (op.args.get('profiles') or ())) for op in source.operations)
    capability = resolve_repair_capability(failed.action if failed else failure.get('action'), failure, {
        'failed_operation': failed.model_dump(mode='json') if failed else None,
        'private_profile': bool(private), 'frozen_acceptance': bool(failure.get('engineering_acceptance'))})
    if not capability.repairable:
        codes = {'unsupported_repair': 'repair_capability_unsupported',
                 'missing_diagnostic': 'repair_diagnostic_unavailable', 'scope_protected': 'repair_scope_protected'}
        messages = {'unsupported_repair': '该操作可执行，但当前没有可安全使用的自动修复补丁。',
                    'missing_diagnostic': '缺少与失败操作一致的修复证据。',
                    'scope_protected': '允许的局部修复字段受到冻结要求保护。'}
        raise FreeCADPlanningError(codes[capability.block_reason],
            messages[capability.block_reason] + ' 原始建模错误：' + str(failure.get('error_message') or '')[:2500])
    return asdict(capability)
