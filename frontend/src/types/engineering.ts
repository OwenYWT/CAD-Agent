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

export type ChangeEvidenceStatus = "changed" | "unchanged" | "unknown";

export interface GeometryMetricChange {
  label: string;
  before: number;
  after: number;
  unit: string;
}

export interface FileChange {
  format: string;
  kind: "added" | "removed" | "replaced";
  beforeUrl?: string;
  afterUrl?: string;
  evidence: "reference_only" | "sha256";
}

export interface ChangeSet {
  id: string;
  panelId: string;
  objective: string | null;
  baseRevisionId: string | null;
  targetRevisionId: string;
  baseVersion: number | null;
  targetVersion: number;
  requestId: string | null;
  taskId: string | null;
  modifiedObjectCount: number | null;
  parameterChanges: ParameterChange[];
  code: {
    status: ChangeEvidenceStatus;
    beforeLines: number | null;
    afterLines: number | null;
  };
  geometry: {
    status: ChangeEvidenceStatus;
    metrics: GeometryMetricChange[];
  };
  files: FileChange[];
  validation: {
    status: "pass" | "warning" | "fail" | "unknown";
    summary: string;
  };
  risk: {
    level: "low" | "medium" | "high" | "unknown";
    reasons: string[];
  };
  agentLogs: string[];
  createdAt: string;
  source?: "snapshot" | "durable";
  reviewStatus?: string;
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
  projectId: string | null;
  name: string;
  updatedAt: string;
}

export interface DurableArtifact {
  id: string;
  revision_id: string;
  artifact_kind: string;
  filename: string;
  content_type: string;
  size_bytes: number;
  sha256: string;
  download_url: string;
  created_at: string;
}

export interface DurableChangeSetDetail {
  id: string;
  project_id: string;
  branch_id: string;
  branch_name: string;
  base_revision_id: string;
  base_revision_number: number;
  base_content_hash: string;
  base_manifest: Record<string, unknown>;
  candidate_revision_id: string;
  candidate_revision_number: number;
  candidate_content_hash: string;
  candidate_manifest: Record<string, unknown>;
  source_workflow_run_id?: string | null;
  objective: string;
  change_summary: Record<string, unknown>;
  validation_summary: Record<string, unknown>;
  risk_summary: Record<string, unknown>;
  status: string;
  workflow_status?: string | null;
  created_at: string;
  updated_at: string;
  review_note?: string | null;
  base_artifacts: DurableArtifact[];
  candidate_artifacts: DurableArtifact[];
  audit_log: Array<{
    id: string;
    action: string;
    payload: Record<string, unknown>;
    occurred_at: string;
  }>;
}

export interface ChangeSetActionResult {
  change_set_id: string;
  status: string;
  replayed: boolean;
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
