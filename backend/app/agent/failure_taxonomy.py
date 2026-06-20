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
    # --- infrastructure (HARD_STOP: retrying against a dead sandbox just burns LLM calls) ---
    FailureClass(
        key="docker_unavailable", label="DockerUnavailable",
        cause="Docker 守护进程不可用", fix_hint="Docker 未运行，无法执行沙箱，请检查部署环境",
        fix_path=FixPath.HARD_STOP, gates=("DockerUnavailable",),
    ),
    FailureClass(
        key="docker_error", label="DockerError",
        cause="Docker 执行错误（镜像缺失/启动失败）", fix_hint="沙箱镜像缺失或启动失败，请检查 cad-agent-sandbox 镜像",
        fix_path=FixPath.HARD_STOP, gates=("DockerError",),
    ),
    FailureClass(
        key="sandbox_no_output", label="RuntimeError",
        cause="沙箱未产出 result.json（执行环境异常）", fix_hint="沙箱未返回结果，可能是执行环境异常",
        fix_path=FixPath.HARD_STOP, gates=("RuntimeError",),
    ),
    # --- timeout: recoverable by simplifying the model ---
    FailureClass(
        key="exec_timeout", label="Timeout / 超时",
        cause="计算过于复杂导致超时", fix_hint="简化模型：减少 fillet 数量、减少布尔运算层数",
        fix_scope="只精简最耗时的部分（减少 fillet/布尔层数），不改变整体设计意图。",
        gates=("TimeoutError",), substrings=("Timeout", "超时"),
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
        cause="shell 壁厚不合理或几何太复杂", fix_hint="减小壁厚；或改用 outer.cut(inner) 方式挖空",
        fix_scope="只改 shell 相关的那几行：改用 outer.cut(inner) 方式挖空。其它行保持不变。",
        substrings=("shell failed", "StdFail_NotDone"),
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
