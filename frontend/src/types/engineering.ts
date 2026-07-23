import type { GenerationResult, ParamConfig } from "../types";

export type AsyncStatus = "idle" | "loading" | "success" | "error";
export type StageStatus = "not_started" | "in_progress" | "awaiting_confirmation" | "completed" | "issue";
export type EngineeringDomain = "overview" | "mechanical" | "electronics" | "simulation" | "firmware";

export interface Project {
  id: string;
  name: string;
  description: string;
  branch: string;
  status: StageStatus;
  updatedAt?: string;
}

export interface EngineeringStage {
  id: "requirements" | "mechanical" | "electronics" | "simulation" | "firmware" | "manufacturing" | "release";
  index: number;
  title: string;
  summary: string;
  status: StageStatus;
  actionLabel: string;
  domain?: EngineeringDomain;
  available: boolean;
  limitation?: string;
}

export interface Task {
  id: string;
  title: string;
  detail: string;
  status: StageStatus;
  startedAt?: number | null;
}

export interface Artifact {
  id: string;
  name: string;
  format: string;
  url: string;
  status: "ready" | "generating" | "failed";
}

export interface Parameter {
  id: string;
  label: string;
  group: "基础尺寸" | "材料" | "装配参数" | "制造参数" | "高级参数";
  value: number;
  defaultValue: number;
  unit: string;
  comment: string;
  min?: number;
  max?: number;
  step?: number;
}

export interface ValidationResult {
  id: string;
  domain: "几何" | "装配" | "DFM" | "ERC / DRC" | "仿真" | "固件" | "文件导出";
  title: string;
  status: "pass" | "warning" | "fail" | "unknown";
  description: string;
  impact?: string;
  object: string;
  suggestion?: string;
}

export interface ParameterChange {
  parameterId: string;
  label: string;
  before: number | string;
  after: number | string;
  unit?: string;
}

export interface ExportJob {
  id: string;
  label: string;
  format: "STEP" | "STL" | "DXF" | "SVG" | "PNG" | "ZIP";
  status: "available" | "generating" | "failed";
  artifact?: Artifact;
  limitation?: string;
}

export interface OnshapeConfig {
  configured: boolean;
  default_document_public: boolean;
}

export interface OnshapeLink {
  request_id: string;
  status: string;
  onshape_url: string;
  document_id: string;
  workspace_id: string;
  element_id?: string | null;
  translation_id?: string | null;
  document_name?: string;
  step_filename?: string;
  mode?: string;
  created_at?: string;
  updated_at?: string;
}

export interface HistoryProject {
  id: string;
  name: string;
  updatedAt: string;
}

export interface EngineeringProjectModel {
  project: Project;
  stages: EngineeringStage[];
  task: Task | null;
  artifacts: Artifact[];
  parameters: Parameter[];
  validation: ValidationResult[];
  exports: ExportJob[];
  result: GenerationResult | null;
}

export type RawParameterMap = Record<string, ParamConfig>;
