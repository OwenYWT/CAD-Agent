import type { EngineeringDomain } from "../../types/engineering";

export const AGENT_CONTEXT_LABELS: Record<EngineeringDomain, string> = {
  overview: "项目流程",
  mechanical: "机械设计",
  electronics: "电子设计",
  simulation: "仿真验证",
  firmware: "固件",
};
