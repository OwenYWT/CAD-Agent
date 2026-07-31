export interface ParamConfig {
  value: number;
  comment: string;
}

export interface ManufacturingProfile {
  process: "fdm" | "sla" | "cnc" | "laser_cut" | "generic";
  material: string;
  nozzle_diameter_mm?: number | null;
  layer_height_mm?: number | null;
  build_volume_mm: number[];
}


export interface CADParameter {
  name: string;
  display_name: string;
  value: number;
  default_value: number;
  type: "number";
  min?: number | null;
  max?: number | null;
  step?: number | null;
  unit?: string | null;
  group?: string | null;
  comment?: string | null;
  line: number;
}

export type StepStatus = "queued" | "running" | "success" | "warn" | "failed" | "skipped";

export interface StepUpdate {
  step: string;
  message: string;
  status?: StepStatus;
  stage_id?: string | null;
  attempt?: number | null;
  started_at?: string | null;
  duration_ms?: number | null;
  detail?: Record<string, unknown> | null;
  part_name?: string;
  part_index?: number;
  total_parts?: number;
}

export interface RunCreatedEvent {
  run_id: string;
  session_id: string;
  panel_id?: string;
  status: "pending" | "running" | "succeeded" | "failed" | "blocked" | "cancelled" | string;
  capability?: string | null;
  user_prompt?: string | null;
  created_at?: string | null;
}

export interface AgentStepEvent {
  run_id?: string | null;
  panel_id?: string;
  step_type: string;
  status: StepStatus | "succeeded" | "pending" | "blocked" | "cancelled" | string;
  message: string;
  started_at?: string | null;
  duration_ms?: number | null;
  detail?: Record<string, unknown> | null;
  legacy_step?: StepUpdate;
}

export interface ArtifactUpdateEvent {
  request_id?: string | null;
  panel_id?: string;
  artifact_type: string;
  path: string;
}

export interface BoundingBox {
  x_min: number;
  x_max: number;
  y_min: number;
  y_max: number;
  z_min: number;
  z_max: number;
}

export interface ValidationData {
  is_watertight: boolean;
  bounding_box: BoundingBox | null;
  volume: number;
  printable?: boolean | null;
  fits_build_volume?: boolean | null;
  min_wall_thickness?: number | null;
  print_warnings?: string[];
}

export interface AssemblyPartInfo {
  name: string;
  description: string;
  code: string;
  status: "success" | "failed";
  position: number[];
  color: string;
}


export interface RecoveryAction {
  label: string;
  prompt: string;
  reason: string;
  action_type: "retry_simpler" | "fix_printability" | "clarify" | "explain" | "inspect";
}

export interface RepairStep {
  attempt: number;
  stage: "validation" | "static_analysis" | "execution" | "geometry" | "vision" | string;
  error_type: string;
  message: string;
  action: string;
  status: "repaired" | "failed" | "skipped" | string;
}

export interface InspectCheck {
  name: string;
  status: "pass" | "warn" | "fail";
  message: string;
  source: "geometry" | "dfm" | "step" | "vision";
}

export interface InspectReport {
  verdict: "pass" | "warn" | "fail";
  printable?: boolean | null;
  is_watertight: boolean;
  bounding_box: BoundingBox | null;
  volume: number;
  min_wall_thickness?: number | null;
  checks: InspectCheck[];
  print_warnings: string[];
  design_score?: number | null;
  dfm_violations?: RuleViolation[];
  available_exports?: string[];
  repair_attempts?: number;
  source?: string;
}

export interface CriticalDimension {
  name: string;
  value?: number | null;
  unit: string;
  reason: string;
}

export interface DesignBrief {
  intent_summary: string;
  artifact_type: string;
  manufacturing_posture: string;
  assumptions: string[];
  critical_dimensions: CriticalDimension[];
  functional_requirements: string[];
  printability_targets: string[];
  acceptance_criteria: string[];
  open_questions: string[];
}

export interface CADPlanBrief {
  description: string;
  part_type: string;
  dimensions: Record<string, number>;
  features: string[];
  constraints?: string[];
  ambiguities?: string[];
  modeling_hint?: string;
  design_brief?: DesignBrief | null;
  manufacturing_profile?: ManufacturingProfile | null;
}

export interface GenerationResult {
  request_id?: string;
  task_id?: string;
  needs_confirmation?: boolean;
  manufacturing_profile?: ManufacturingProfile | null;
  snapshot_id?: string;
  project_id?: string;
  branch_id?: string;
  expected_base_revision_id?: string;
  revision_id?: string;
  workflow_run_id?: string;
  change_set_id?: string;
  version?: number;
  success: boolean;
  files?: Record<string, string>;
  code?: string;
  params?: Record<string, ParamConfig>;
  parameters?: CADParameter[] | null;
  execution_time_ms?: number;
  attempts?: number;
  repair_history?: RepairStep[];
  recovery_actions?: RecoveryAction[];
  error?: { type: string; message: string };
  validation?: ValidationData;
  assembly_parts?: AssemblyPartInfo[];
  inspect_report?: InspectReport | null;
  plan?: CADPlanBrief | null;
  design_brief?: DesignBrief | null;
}

export interface ModelSnapshotSummary {
  id: string;
  project_id?: string;
  branch_id?: string;
  revision_id?: string;
  panel_id: string;
  parent_snapshot_id?: string | null;
  version: number;
  source: "generation" | "execute_code" | "parameter_edit" | "modify_part" | string;
  prompt: string;
  status: "pass" | "warn" | "fail" | "unknown";
  created_at: string;
  inspect_verdict?: "pass" | "warn" | "fail" | null;
  available_exports?: string[];
}

export interface ModelSnapshotDetail extends ModelSnapshotSummary {
  code: string;
  result: GenerationResult;
  files?: Record<string, string>;
  params?: Record<string, ParamConfig> | null;
  parameters?: CADParameter[] | null;
  validation?: ValidationData | null;
  inspect_report?: InspectReport | null;
  repair_history?: RepairStep[];
}

export interface ChatMessage {
  role: "user" | "assistant";
  content: string;
  result?: GenerationResult;
}

export interface MultiStepInfo {
  phase: string;
  description: string;
  status: "pending" | "running" | "done" | "error";
}

export interface DFMIssue {
  category: string;
  severity: "critical" | "warning" | "info";
  description: string;
  suggestion: string;
  location: string;
}

export interface RuleViolation {
  rule_id: string;
  process: string;
  category: string;
  severity: string;
  source: "geometric" | "heuristic";
  actual_value: number | null;
  message: string;
  suggestion: string;
}

export interface DFMRuleData {
  id: string;
  process: string;
  category: string;
  check_type: string;
  threshold_min: number | null;
  threshold_max: number | null;
  unit: string;
  severity: string;
  description: string;
  suggestion_template: string;
  enabled: boolean;
}

export interface StepAnalysisSummary {
  available: boolean;
  face_count: number;
  edge_count: number;
  feature_count: number;
  min_wall_thickness: number | null;
  min_fillet_radius: number | null;
  min_hole_diameter: number | null;
  min_draft_angle: number | null;
  features: { type: string; dimensions: Record<string, unknown>; center: number[] }[];
}

export interface DesignAnalysis {
  design_score: number;
  design_summary: string;
  structural_issues: string[];
  functional_notes: string[];
  recommended_process: string;
  process_compatibility: Record<string, string>;
  dfm_issues: DFMIssue[];
  estimated_difficulty: string;
  geometry: Record<string, unknown>;
  rule_violations: RuleViolation[];
  step_analysis?: StepAnalysisSummary;
  annotations?: Annotation3D[];
}

export interface Annotation3D {
  id: string;
  type: "point_marker" | "face_highlight" | "dimension";
  severity: "critical" | "warning" | "info";
  position: [number, number, number];
  normal?: [number, number, number] | null;
  label: string;
  detail: string;
  category: string;
  face_ids?: number[] | null;
}

export type WSMessage =
  | { type: "step_update"; data: StepUpdate & { panel_id?: string } }
  | { type: "run_created"; data: RunCreatedEvent }
  | { type: "agent_step"; data: AgentStepEvent }
  | { type: "artifact_update"; data: ArtifactUpdateEvent }
  | { type: "diagnostics"; data: { panel_id?: string; message: string; detail?: Record<string, unknown> | null } }
  | { type: "generation_result"; data: GenerationResult & { panel_id?: string } }
  | {
      type: "task_status";
      data: {
        task_id?: string | null;
        panel_id?: string;
        status: string;
      };
    }
  | { type: "assistant_message"; data: { content: string } };

export interface DurableTaskEvent {
  id: string;
  workflow_run_id: string;
  sequence: number;
  event_type: string;
  payload: Record<string, unknown>;
  occurred_at: string;
}

export interface DurableTaskSnapshot {
  id: string;
  project_id: string;
  kind: string;
  status: string;
  request_payload: {
    branch_id?: string;
    expected_base_revision_id?: string;
    [key: string]: unknown;
  };
  last_event_sequence: number;
  error_code?: string | null;
  error_message?: string | null;
  change_set?: {
    id: string;
    status: string;
    base_revision_id: string;
    candidate_revision_id: string;
    objective: string;
  } | null;
}

export type DurableWSMessage =
  | { type: "task_snapshot"; data: DurableTaskSnapshot }
  | { type: "task_event"; data: DurableTaskEvent }
  | {
      type: "task_stream_complete";
      data: {
        workflow_run_id: string;
        status: string;
        last_event_sequence: number;
      };
    }
  | {
      type: "cursor_expired";
      data: { earliest_sequence: number; current_sequence: number };
    };

export type CapabilityId =
  | "cad"
  | "cad-viewer"
  | "step-parts"
  | "dxf"
  | "urdf"
  | "srdf"
  | "sdf"
  | "sendcutsend"
  | "gcode"
  | "bambu-labs"
  | "implicit-cad";

/** Capabilities currently routed by the conversational WebSocket entry point. */
export type ChatCapabilityId = Extract<CapabilityId, "cad" | "dxf">;
export type CapabilitySelection = "auto" | ChatCapabilityId;

export type CapabilityRiskLevel =
  | "read_only"
  | "compute"
  | "external_write"
  | "physical_action";

export interface CapabilityAction {
  id: string;
  name: string;
  mode?: CapabilityRiskLevel;
  requires_confirmation?: boolean;
  available?: boolean;
  blocked_reason?: string | null;
}

export interface CapabilityDependency {
  id: string;
  label: string;
  kind: string;
  required: boolean;
  available?: boolean;
  detail?: string | null;
}

export interface CapabilityDefinition {
  id: CapabilityId;
  name: string;
  group: string;
  summary: string;
  maturity: "stable" | "beta" | "experimental";
  risk_level: CapabilityRiskLevel;
  actions: CapabilityAction[];
  accepts: string[];
  produces: string[];
  dependencies: CapabilityDependency[];
  available?: boolean;
  blocked_reasons?: string[];
  upstream?: {
    version: string;
    commit: string;
    url: string;
  };
}
