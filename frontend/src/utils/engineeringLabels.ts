const STATUS_LABELS: Record<string, string> = {
  pass: "通过",
  warn: "警告",
  fail: "失败",
  unknown: "未知",
};

const SOURCE_LABELS: Record<string, string> = {
  initial: "项目初始化",
  generation: "自然语言生成",
  execute_code: "参数化代码执行",
  parameter_edit: "参数修改",
  modify_part: "零件修改",
  geometry: "几何检查",
  geometry_validator: "几何验证器",
  vision: "视觉检查",
  dfm: "制造检查",
};

const CHECK_LABELS: Record<string, string> = {
  watertight: "水密性",
  dimension_range: "尺寸范围",
  build_volume: "成型空间",
  min_wall: "最小壁厚",
  expected_dimensions: "期望尺寸",
  volume: "体积",
  face_count: "面数",
  vision: "视觉检查",
};

const PROMPT_LABELS: Record<string, string> = {
  "manual code execution": "手动执行参数化建模代码",
};

const TASK_EVENT_LABELS: Record<string, string> = {
  "workflow.created": "持久任务已创建",
  "workflow.state_changed": "任务状态已更新",
  "workflow.cancellation_requested": "已请求取消任务",
  "workflow.plan_recorded": "建模计划已记录",
  "workflow.confirmed": "任务确认已记录",
  "agent.requirements.completed": "工程需求已解析",
  "agent.decomposition.completed": "建模步骤已拆解",
  "agent.plan.completed": "执行计划已生成",
  "agent.candidate_build.created": "候选模型已开始构建",
  "agent.source.generated": "建模代码已生成",
  "agent.repair.source_generated": "自动修复代码已生成",
  "agent.visual_repair.source_generated": "视觉修复代码已生成",
  "agent.staging_manifest.accepted": "候选工程产物已生成",
  "agent.validation_evidence.recorded": "工程验证证据已记录",
  "agent.candidate.sealed": "候选版本已封装",
  "agent.candidate_build.state_changed": "候选模型状态已更新",
  "step.created": "执行步骤已创建",
  "step.state_changed": "步骤状态已更新",
  "attempt.created": "执行尝试已创建",
  "attempt.state_changed": "执行尝试状态已更新",
  "attempt.leased": "计算资源已分配",
  "attempt.started": "隔离计算已开始",
  "attempt.heartbeat": "隔离计算运行中",
  "attempt.completed": "隔离计算已完成",
  "source.preparation_started": "正在准备建模代码",
  "source.prepared": "建模代码已准备",
  "artifact.committed": "工程产物已保存",
  "artifact.rejected": "工程产物校验失败",
  "validation.completed": "工程验证已完成",
  "change_set.evidence_updated": "变更证据已更新",
  "change_set.accepted": "变更已接受",
  "change_set.changes_requested": "已请求修改变更",
  "change_set.rejected": "变更已拒绝",
  "change_set.committed": "版本已提交",
  "change_set.rolled_back": "版本已回滚",
  "compat.progress": "兼容任务进度已更新",
  "compat.result": "兼容任务结果已更新",
  "compat.state": "兼容任务状态已更新",
};

function displayLabel(value: string | null | undefined, labels: Record<string, string>, fallback: string) {
  const normalized = value?.trim();
  if (!normalized) return fallback;
  return labels[normalized] || normalized;
}

export function engineeringStatusLabel(value: string | null | undefined) {
  return displayLabel(value, STATUS_LABELS, "未知");
}

export function engineeringSourceLabel(value: string | null | undefined) {
  return displayLabel(value, SOURCE_LABELS, "未记录");
}

export function engineeringCheckLabel(value: string | null | undefined) {
  return displayLabel(value, CHECK_LABELS, "未命名检查");
}

export function engineeringPromptLabel(value: string | null | undefined) {
  return displayLabel(value, PROMPT_LABELS, "未记录提示词");
}

export function engineeringTaskEventLabel(
  value: string | null | undefined,
) {
  return displayLabel(value, TASK_EVENT_LABELS, "任务事件");
}
