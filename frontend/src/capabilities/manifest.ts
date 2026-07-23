import type { CapabilityDefinition, CapabilityId } from "../types";

const commonUpstream = {
  version: "0.3.9",
  commit: "fdbb4b4fb62d95ae298cfe9a46fdc7092bdaf423",
  url: "https://www.cadskills.xyz/",
};

export const CAPABILITY_GROUPS = ["建模与制图", "查看与复用", "机器人与仿真", "制造与打印"] as const;

export const LOCAL_CAPABILITIES: CapabilityDefinition[] = [
  {
    id: "cad", name: "CAD", group: "建模与制图", maturity: "stable", risk_level: "compute",
    summary: "STEP-first 参数化零件与装配的创建、修改、检查和快照验证。",
    actions: [{ id: "generate", name: "生成", mode: "compute" }, { id: "inspect", name: "检查", mode: "read_only" }, { id: "snapshot", name: "快照", mode: "compute" }, { id: "export", name: "导出", mode: "compute" }],
    accepts: ["text/plain", ".step", ".stp", ".py"], produces: [".py", ".step", ".stp", ".stl", ".3mf", ".glb", ".png", ".gif"], dependencies: [], upstream: commonUpstream,
  },
  {
    id: "dxf", name: "DXF", group: "建模与制图", maturity: "stable", risk_level: "compute",
    summary: "生成和检查 2D 轮廓、模板、面板、垫片与切割图。",
    actions: [{ id: "generate", name: "生成", mode: "compute" }, { id: "validate", name: "验证几何", mode: "read_only" }],
    accepts: ["text/plain", ".py", ".step", ".stp", ".dxf"], produces: [".py", ".dxf"], dependencies: [], upstream: commonUpstream,
  },
  {
    id: "implicit-cad", name: "Implicit CAD", group: "建模与制图", maturity: "experimental", risk_level: "compute",
    summary: "实验性的 GLSL SDF、晶格与 TPMS 建模、raymarch 预览和网格导出。",
    actions: [{ id: "snapshot", name: "快照", mode: "compute" }, { id: "export", name: "导出", mode: "compute" }],
    accepts: ["text/plain", ".implicit.js", ".implicit.mjs"], produces: [".implicit.js", ".implicit.mjs", ".glb", ".stl", ".3mf", ".png", ".gif"],
    dependencies: [{ id: "node", label: "Node.js", kind: "executable", required: true }], upstream: commonUpstream,
  },
  {
    id: "cad-viewer", name: "CAD Viewer", group: "查看与复用", maturity: "stable", risk_level: "compute",
    summary: "统一预览 CAD、DXF、G-code、隐式 CAD 与机器人描述文件。",
    actions: [{ id: "start", name: "启动或复用查看器", mode: "compute" }, { id: "review", name: "创建审阅链接", mode: "read_only" }],
    accepts: [".step", ".stp", ".stl", ".3mf", ".glb", ".dxf", ".gcode", ".urdf", ".srdf", ".sdf", ".implicit.js", ".implicit.mjs"], produces: ["text/html", "http-url"],
    dependencies: [{ id: "node", label: "Node.js", kind: "executable", required: true }], upstream: commonUpstream,
  },
  {
    id: "step-parts", name: "step.parts", group: "查看与复用", maturity: "stable", risk_level: "external_write",
    summary: "检索并校验螺钉、轴承、电机、连接器等商品 STEP 零件。",
    actions: [{ id: "search", name: "搜索", mode: "read_only" }, { id: "download", name: "下载并校验", mode: "external_write" }],
    accepts: ["text/plain", "part-id"], produces: ["application/json", ".step"],
    dependencies: [{ id: "step-parts-api", label: "api.step.parts", kind: "network", required: true }], upstream: commonUpstream,
  },
  {
    id: "urdf", name: "URDF", group: "机器人与仿真", maturity: "stable", risk_level: "compute",
    summary: "机器人 link、joint、limit、inertial、visual/collision 与网格引用。",
    actions: [{ id: "generate", name: "生成并验证", mode: "compute" }],
    accepts: ["text/plain", ".py", ".urdf", ".step", ".stp", ".stl", ".dae", ".obj"], produces: [".py", ".urdf"], dependencies: [], upstream: commonUpstream,
  },
  {
    id: "srdf", name: "SRDF / MoveIt", group: "机器人与仿真", maturity: "stable", risk_level: "compute",
    summary: "MoveIt 规划组、末端执行器、姿态与碰撞禁用语义。",
    actions: [{ id: "generate", name: "生成并验证", mode: "compute" }],
    accepts: ["text/plain", ".py", ".srdf", ".urdf"], produces: [".py", ".srdf"], dependencies: [], upstream: commonUpstream,
  },
  {
    id: "sdf", name: "SDF / Gazebo", group: "机器人与仿真", maturity: "stable", risk_level: "compute",
    summary: "SDFormat 模型与世界，包括物理、传感器、灯光和插件。",
    actions: [{ id: "generate", name: "生成并验证", mode: "compute" }, { id: "gz-check", name: "Gazebo 检查", mode: "read_only" }],
    accepts: ["text/plain", ".py", ".sdf", ".urdf", ".step", ".stp", ".stl", ".dae", ".obj"], produces: [".py", ".sdf"],
    dependencies: [{ id: "gz", label: "Gazebo gz（可选）", kind: "executable", required: false }], upstream: commonUpstream,
  },
  {
    id: "sendcutsend", name: "SendCutSend", group: "制造与打印", maturity: "beta", risk_level: "read_only",
    summary: "基于当前官方材料、厚度和工艺规则的 DXF/STEP 上传预检。",
    actions: [{ id: "preflight", name: "上传预检", mode: "read_only" }],
    accepts: [".dxf", ".step", ".stp", "material-sku", "service-selection"], produces: ["application/json", "text/markdown"],
    dependencies: [{ id: "sendcutsend-specs", label: "SendCutSend 官方规格", kind: "network", required: true }], upstream: commonUpstream,
  },
  {
    id: "gcode", name: "G-code", group: "制造与打印", maturity: "beta", risk_level: "compute",
    summary: "调用真实切片器 dry-run、切片并静态验证 FDM G-code。",
    actions: [{ id: "discover", name: "发现切片器", mode: "read_only" }, { id: "inspect", name: "检查网格", mode: "read_only" }, { id: "dry-run", name: "切片预演", mode: "read_only" }, { id: "slice", name: "切片", mode: "compute" }, { id: "validate", name: "验证", mode: "read_only" }],
    accepts: [".stl", ".obj", ".3mf", ".ply", ".glb", ".gltf", ".gcode", ".json"], produces: [".gcode", "application/json"],
    dependencies: [{ id: "slicer", label: "OrcaSlicer / PrusaSlicer / CuraEngine", kind: "executable", required: true }], upstream: commonUpstream,
  },
  {
    id: "bambu-labs", name: "Bambu Labs", group: "制造与打印", maturity: "beta", risk_level: "physical_action",
    summary: "局域网读取状态、上传并在显式确认后谨慎启停 Bambu 打印任务。",
    actions: [{ id: "dry-run", name: "交接预演", mode: "read_only" }, { id: "serial", name: "读取序列号", mode: "read_only", requires_confirmation: true }, { id: "status", name: "读取状态", mode: "read_only" }, { id: "upload", name: "上传", mode: "external_write", requires_confirmation: true }, { id: "start-print", name: "启印", mode: "physical_action", requires_confirmation: true }, { id: "pause-print", name: "暂停", mode: "physical_action", requires_confirmation: true }, { id: "cancel-print", name: "取消", mode: "physical_action", requires_confirmation: true }, { id: "clear-error", name: "清除错误", mode: "external_write", requires_confirmation: true }],
    accepts: [".gcode", ".gcode.3mf", ".json"], produces: [".gcode.3mf", "application/json", "printer-job"],
    dependencies: [{ id: "bambu-lan", label: "Bambu LAN / FTPS / MQTT", kind: "lan_device", required: true }], upstream: commonUpstream,
  },
];

export function findLocalCapability(id: CapabilityId): CapabilityDefinition | undefined {
  return LOCAL_CAPABILITIES.find((capability) => capability.id === id);
}
