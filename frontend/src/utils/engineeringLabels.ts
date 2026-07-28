const STATUS_LABELS: Record<string, string> = {
  pass: "通过",
  warn: "警告",
  fail: "失败",
  unknown: "未知",
};

const SOURCE_LABELS: Record<string, string> = {
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
