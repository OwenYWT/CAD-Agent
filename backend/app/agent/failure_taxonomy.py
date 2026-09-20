"""Central failure taxonomy (A1) — one source of truth for how a generation/execution
failure is classified, what targeted fix hint it gets, and how the retry loop should
react (which fix path, per-class retry budget, recoverable vs hard-stop).

Replaces the duplicated CodeGenerator._ERROR_HINTS list and the hand-maintained markdown
table inside ERROR_FIX_PROMPT — both are now generated from FAILURE_CLASSES.

stdlib only; classify() is a pure function (trivially unit-testable).
"""

from dataclasses import dataclass, field
from enum import Enum


class FixPath(Enum):
    CODE = "code"            # → CodeGenerator.fix_error (the default)
    VISUAL = "visual"        # → CodeGenerator.fix_visual_issues
    HARD_STOP = "hard_stop"  # non-recoverable (infra) — abort retries, do not call the LLM


@dataclass(frozen=True)
class FailureClass:
    key: str                 # stable id; also the oscillation-guard key
    label: str               # the "错误关键词" column of the generated table
    cause: str               # the "原因" column
    fix_hint: str            # the "修复方法" column + the runtime hint appended to fix_error
    fix_scope: str = ""      # A3 minimal-change directive (which lines to touch)
    fix_path: FixPath = FixPath.CODE
    retry_budget: int | None = None        # None = use the global MAX_RETRIES loop
    substrings: tuple[str, ...] = ()        # matched (case-insensitive) on message+traceback
    gates: tuple[str, ...] = ()             # matched on the source gate / infra error_type


# Declaration order = precedence. Gate-anchored classes come first so an orchestrator
# gate literal (e.g. "GeometryError") never collides with a substring match; then the
# substring classes in the legacy _ERROR_HINTS order; `unknown` is the catch-all.
FAILURE_CLASSES: list[FailureClass] = [
    FailureClass(
        key="sketch_redundant_constraints", label="Sketch redundant constraints",
        cause="草图包含重复限制自由度的约束",
        fix_hint="依据求解器诊断移除多余几何关系，保留已有尺寸和其他建模操作，再由求解器验证",
        fix_scope="只调整报错草图的非尺寸约束；不得删除固定编号约束或改变尺寸来绕过错误",
        retry_budget=2, gates=("sketch_redundant_constraints",),
    ),
    FailureClass(
        key="sketch_conflicting_constraints", label="Sketch conflicting constraints",
        cause="草图约束无法同时满足",
        fix_hint="检查报错草图的几何关系；保留数值尺寸，尺寸本身冲突时停止并报告冲突",
        fix_scope="只调整报错草图的非尺寸约束，不得猜测并更改用户尺寸",
        retry_budget=2, gates=("sketch_conflicting_constraints",),
    ),
    FailureClass(
        key="sketch_under_constrained", label="Sketch under constrained",
        cause="草图尚有未约束自由度",
        fix_hint="依据已有几何和需求补齐缺失约束，不改已有尺寸或其他特征",
        fix_scope="只补充报错草图缺失的约束，重新运行求解器确认",
        retry_budget=2, gates=("sketch_under_constrained",),
    ),
    FailureClass(
        key="dfm_violation", label="DFMError",
        cause="真实制造规则检查发现几何问题",
        fix_hint="根据实际规则、测量值和建议修改未被用户锁定的制造特征，随后重新执行全部校验",
        fix_scope="保留用户硬约束、孔数量、孔位和关键尺寸；无法兼容时明确失败，不放宽规则",
        fix_path=FixPath.CODE, retry_budget=2, gates=("DFMError",),
    ),
    # --- model-provider failures cannot be repaired by changing CAD code ---
    FailureClass(
        key="provider_configuration",
        label="ProviderConfigurationError",
        cause="模型服务额度、认证、权限或配置不可用",
        fix_hint="请管理员修复模型服务额度、凭据、权限或模型配置",
        fix_path=FixPath.HARD_STOP,
        retry_budget=0,
        gates=(
            "ProviderQuotaError",
            "ProviderAuthenticationError",
            "ProviderPermissionError",
            "ProviderConfigurationError",
        ),
    ),
    FailureClass(
        key="provider_temporarily_unavailable",
        label="ProviderUnavailableError",
        cause="模型服务限流、网络异常或暂时不可用",
        fix_hint="请稍后重新提交，不要修改 CAD 代码",
        fix_path=FixPath.HARD_STOP,
        retry_budget=0,
        gates=(
            "ProviderRateLimitError",
            "ProviderConnectionError",
            "ProviderUnavailableError",
        ),
    ),
    # --- non-recoverable execution failures ---
    FailureClass(
        key="sandbox_unavailable",
        label="SandboxUnavailable",
        cause="隔离计算运行时不可用",
        fix_hint="MCAD 计算服务当前不可用，请检查执行后端和固定运行时镜像",
        fix_path=FixPath.HARD_STOP,
        retry_budget=0,
        gates=("SandboxUnavailable", "DockerUnavailable"),
    ),
    FailureClass(
        key="container_launch",
        label="ContainerLaunchError",
        cause="隔离计算任务启动失败",
        fix_hint="MCAD Worker 启动失败，请检查执行器容量、镜像和沙箱配置",
        fix_path=FixPath.HARD_STOP,
        retry_budget=0,
        gates=("ContainerLaunchError", "DockerError", "PodmanError"),
    ),
    FailureClass(
        key="artifact_rejected",
        label="ArtifactRejected",
        cause="执行产物缺失、损坏或未通过完整性校验",
        fix_hint="产物校验失败，请检查 Worker 输出契约和对象存储链路",
        fix_path=FixPath.HARD_STOP,
        retry_budget=0,
        gates=("ArtifactRejected",),
    ),
    FailureClass(
        key="execution_cancelled",
        label="ExecutionCancelled",
        cause="任务已被取消",
        fix_hint="任务已取消；如仍需要结果，请由用户明确重新提交",
        fix_path=FixPath.HARD_STOP,
        retry_budget=0,
        gates=("ExecutionCancelled",),
    ),
    # --- timeout: recoverable by simplifying the model ---
    FailureClass(
        key="exec_timeout", label="Timeout / 超时",
        cause="计算过于复杂导致超时", fix_hint="简化模型：减少 fillet 数量、减少布尔运算层数",
        fix_scope="只精简最耗时的部分（减少 fillet/布尔层数），不改变整体设计意图。",
        retry_budget=1,
        gates=("ExecutionTimeout", "TimeoutError"),
        substrings=("Timeout", "超时"),
    ),
    FailureClass(
        key="exec_oom",
        label="ExecutionOOM",
        cause="模型计算超过内存上限",
        fix_hint="简化模型：减少高密度网格、复杂布尔和一次性特征数量",
        fix_scope="只精简造成内存峰值的网格或布尔步骤，不改变关键尺寸和设计意图。",
        retry_budget=1,
        gates=("ExecutionOOM", "MemoryError", "OOMError", "OutOfMemoryError"),
    ),
    FailureClass(
        key="invalid_code",
        label="InvalidCode",
        cause="生成代码不符合执行契约",
        fix_hint="根据语法、导入或结果对象错误修复代码",
        fix_scope="只修复执行器明确指出的代码问题，不改变无关设计。",
        retry_budget=2,
        gates=("InvalidCode",),
    ),
    FailureClass(
        key="cad_kernel",
        label="CADKernelError",
        cause="CAD 内核无法完成当前几何操作",
        fix_hint="根据内核错误调整失败的几何操作",
        fix_scope="只调整失败的布尔、圆角、倒角、放样或选择器，不重写完整模型。",
        retry_budget=2,
        gates=("CADKernelError",),
    ),
    # --- gate-anchored validation classes ---
    FailureClass(
        key="static_analysis", label="StaticAnalysis",
        cause="代码静态分析发现的问题", fix_hint="按提示修复具体问题",
        fix_scope="只针对静态分析指出的具体行做最小修改。",
        gates=("StaticAnalysis",),
    ),
    FailureClass(
        key="geometry_invalid", label="GeometryError",
        cause="几何验证失败（非水密/退化/尺寸超限）", fix_hint="检查模型是否水密、是否退化、尺寸是否合理",
        fix_scope="针对几何验证报告的具体问题做最小修改（如补水密、修正尺寸），保留其余设计。",
        retry_budget=2,
        gates=("GeometryError",),
    ),
    # --- OCCT / CadQuery substring classes (union of _ERROR_HINTS + ERROR_FIX_PROMPT rows) ---
    FailureClass(
        key="empty_stack_boolean", label="at least one solid on the stack",
        cause="空工作平面上做布尔运算", fix_hint="先 .extrude()/.revolve() 创建实体再 .union()/.cut()",
        fix_scope="只改创建首个实体的那几行：先 extrude/revolve 出实体再 union/cut。其它行不动。",
        substrings=("at least one solid on the stack",),
    ),
    FailureClass(
        key="fillet_too_large", label="BRep_API: not done",
        cause="fillet/chamfer 半径过大", fix_hint="减小到最短边的 25%，或直接删掉 .fillet() 调用",
        fix_scope="只改 .fillet()/.chamfer() 这一行：删除该调用或把半径降到最短边的 25%。不要重写其它部分。",
        substrings=("BRep_API: not done",),
    ),
    FailureClass(
        key="shell_failed", label="shell failed / StdFail_NotDone",
        cause="抽壳偏移无法构造有效实体", fix_hint="保持用户指定壁厚和外形，改用 outer.cut(inner) 挖腔；不要擅自减小壁厚",
        fix_scope="只改 shell 相关的那几行：改用 outer.cut(inner) 方式挖空。其它行保持不变。",
        substrings=("shell failed", "StdFail_NotDone", "Standard_ConstructionError"),
    ),
    FailureClass(
        key="wire_not_closed", label="Wire is not closed",
        cause="轮廓未闭合", fix_hint="在 .extrude()/.revolve() 前加 .close()",
        fix_scope="只在 .extrude()/.revolve() 前插入 .close()。不要改动其它行。",
        substrings=("Wire is not closed",),
    ),
    FailureClass(
        key="revolve_crosses_axis", label="Geom_UndefinedDerivative",
        cause="revolve 截面跨越旋转轴", fix_hint="所有截面点 X 坐标必须 >= 0",
        fix_scope="只调整 revolve 截面坐标使所有点 X>=0。其它行保持不变。",
        substrings=("Geom_UndefinedDerivative",),
    ),
    FailureClass(
        key="loft_insufficient_wires", label="not enough wires",
        cause="loft/sweep 缺少截面或路径", fix_hint="确保有 >=2 个截面 (loft) 或有路径 (sweep)",
        fix_scope="只补齐缺失的截面/路径，使 loft 有 >=2 个截面。其它行不动。",
        substrings=("not enough wires",),
    ),
    FailureClass(
        key="null_selector", label="Standard_NullObject",
        cause="面/边选择器返回空", fix_hint='检查 ">Z" vs "<Z"，确认几何体存在该面',
        fix_scope="只改受影响的面/边选择器（如 >Z↔<Z）。其它行保持不变。",
        substrings=("Standard_NullObject",),
    ),
    FailureClass(
        key="disallowed_import", label="ModuleNotFoundError",
        cause="导入了不允许的模块", fix_hint="只用 cadquery, math, numpy",
        fix_scope="只删除/替换不允许的 import 行。其它逻辑不动。",
        substrings=("ModuleNotFoundError", "禁止导入", "禁止调用", "禁止访问"),
    ),
    FailureClass(
        key="syntax_error", label="SyntaxError",
        cause="Python 语法错误", fix_hint="修复语法",
        fix_scope="只修复报错位置的语法。其它行保持不变。",
        substrings=("SyntaxError", "语法错误"),
    ),
    FailureClass(
        key="infinite_recursion", label="recursion / maximum recursion",
        cause="无限递归", fix_hint="检查循环引用或过深的布尔链",
        fix_scope="只打断递归/过深布尔链的那部分。其它设计保留。",
        substrings=("recursion", "maximum recursion"),
    ),
    # --- vision (handled via the visual fix path, separate from code fixes) ---
    FailureClass(
        key="vision_mismatch", label="vision mismatch",
        cause="视觉校验发现与描述不符", fix_hint="按视觉问题修复（合并游离部分、修正形状/比例/特征）",
        fix_path=FixPath.VISUAL, retry_budget=2, gates=("vision_mismatch",),
    ),
    FailureClass(
        key="vision_indeterminate", label="vision indeterminate",
        cause="视觉校验无法执行/结果不可信", fix_hint="无渲染图或解析失败，视觉校验未执行（不影响模型产出）",
        fix_path=FixPath.HARD_STOP, gates=("vision_indeterminate",),
    ),
    # --- catch-all ---
    FailureClass(
        key="unknown", label="未知错误",
        cause="未归类的错误", fix_hint="检查 traceback，按报错信息定位问题",
        fix_scope="只针对 traceback 指出的位置做最小修改。其它行保持不变。",
    ),
]

_BY_KEY = {fc.key: fc for fc in FAILURE_CLASSES}
UNKNOWN = _BY_KEY["unknown"]


def classify(
    error_type: str | None,
    message: str | None,
    traceback: str | None = None,
    gate: str | None = None,
) -> FailureClass:
    """Classify a raw failure into a FailureClass. Deterministic.

    Precedence: gate-anchored match first (gate string OR error_type matched against a
    class's `gates`), then substring scan over message+traceback in declaration order,
    then `unknown`.
    """
    gate_tokens = [t for t in (gate, error_type) if t]
    for fc in FAILURE_CLASSES:
        if fc.gates and any(tok in fc.gates for tok in gate_tokens):
            return fc

    haystack = f"{message or ''}\n{traceback or ''}".lower()
    for fc in FAILURE_CLASSES:
        if fc.substrings and any(s.lower() in haystack for s in fc.substrings):
            return fc

    return UNKNOWN


def fix_hint_for(fc: FailureClass) -> str:
    """The targeted hint appended to the fix_error user message (A1 + A3 directive)."""
    parts = [fc.fix_hint]
    if fc.fix_scope:
        parts.append(fc.fix_scope)
    return " ".join(parts)


def render_prompt_table() -> str:
    """Generate the error-pattern lookup table embedded in ERROR_FIX_PROMPT.

    Only the OCCT/Python code-fix classes are listed (the ones a code fix can act on);
    infra/vision classes are not LLM-fixable and are excluded from the prompt table.
    """
    rows = ["| 错误关键词 | 原因 | 修复方法 |", "|-----------|------|---------|"]
    for fc in FAILURE_CLASSES:
        if fc.fix_path is not FixPath.CODE or fc.key == "unknown":
            continue
        rows.append(f"| {fc.label} | {fc.cause} | {fc.fix_hint} |")
    return "\n".join(rows)
