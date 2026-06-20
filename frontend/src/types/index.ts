export interface ParamConfig {
  value: number;
  comment: string;
}

export interface StepUpdate {
  step: string;
  message: string;
  part_name?: string;
  part_index?: number;
  total_parts?: number;
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
}

export interface CADPlanBrief {
  description: string;
  part_type: string;
  dimensions: Record<string, number>;
  features: string[];
  constraints?: string[];
  ambiguities?: string[];
  modeling_hint?: string;
}

export interface GenerationResult {
  request_id?: string;
  success: boolean;
  files?: Record<string, string>;
  code?: string;
  params?: Record<string, ParamConfig>;
  execution_time_ms?: number;
  attempts?: number;
  error?: { type: string; message: string };
  validation?: ValidationData;
  assembly_parts?: AssemblyPartInfo[];
  inspect_report?: InspectReport | null;
  plan?: CADPlanBrief | null;
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
  | { type: "generation_result"; data: GenerationResult & { panel_id?: string } }
  | { type: "assistant_message"; data: { content: string } };
