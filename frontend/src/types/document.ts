export interface SemanticParameter {
  id: string; object_name: string; property_name: string; label: string;
  value: number; unit: string | null; editable: boolean;
  minimum: number | null; maximum: number | null; step: number | null;
}

export interface SemanticFeature {
  id: string; kernel_name: string; label: string; type: string | null;
  dependencies: string[]; parameters: SemanticParameter[]; is_valid: boolean | null;
  role: string | null; intent: string | null;
  annotation_version?: number; annotation_source?: "user";
  revision_created?: string | null; last_modified?: string | null;
  topology_bindings?: Array<{ schema_version: "topology-selector.v1"; backend: "freecad";
    revision_id: string; object_name: string; subelement_kind: "face" | "edge";
    geometry: "planar" | "circular"; axis: "x" | "y" | "z"; extreme: "min" | "max";
    radius_mm?: number; center?: { x: number; y: number; z: number }; tolerance_mm: number }>;
  shape: { volume?: number; faces?: number; edges?: number } | null;
  sketch: { fully_constrained?: boolean; solve_status?: number } | null;
  instance?: { source: string; translation_mm: number[]; rotation_axis: number[]; rotation_deg: number } | null;
}

export interface DocumentArtifact { artifact_id: string; sha256: string; url: string; size_bytes: number }

export type InspectionFieldName = "properties" | "constraints" | "geometry" | "topology" | "dependencies";
export interface InspectionPage {
  status: "measured" | "unavailable"; reason?: string; items?: unknown[];
  total?: number; returned?: number; recorded?: number; offset?: number; omitted?: number;
}
export interface KernelInspection {
  revision_id: string; artifact_sha256: string;
  objects: Array<{ name: string; error?: string } & Partial<Record<InspectionFieldName, InspectionPage>>>;
}

export interface CloudDocument {
  document_id: string; project_id: string; active_branch_id: string;
  head_revision_id: string; revision_id: string; state_version: number; event_sequence: number;
  can_edit: boolean; can_share: boolean; features: SemanticFeature[]; roots: string[];
  can_commit?: boolean;
  mesh: DocumentArtifact | null; fcstd: DocumentArtifact | null; state: DocumentArtifact | null;
  parameter_state_sha256: string | null; modeling_backend: string | null;
}

export interface DocumentDelta extends Pick<CloudDocument, "mesh" | "fcstd" | "state" | "modeling_backend" | "parameter_state_sha256" | "roots"> {
  base_revision_id: string; revision_id: string; base_state_version: number; state_version: number;
  upserted: SemanticFeature[]; removed: string[];
}

export interface FeatureAnnotationDelta {
  feature_id: string; role: string | null; intent: string | null;
  annotation_version: number; annotation_source: "user";
}
export interface DocumentEvent { sequence: number; event_type: string; payload: DocumentDelta | FeatureAnnotationDelta }
export interface DocumentOperation {
  id: string; action: string; status: string; objective: string; error_code: string | null;
  result_revision_id: string | null; change_set_id: string | null;
  created_at: string; started_at: string | null; finished_at: string | null;
}
export interface DocumentCollaboration {
  operations: DocumentOperation[];
  leases?: Array<{ feature_id: string; principal_id: string; display_name: string | null; expires_at: string }>;
  comments: Array<{ id: string; body: string; principal_id: string; display_name: string | null; feature_id: string | null; revision_id: string; created_at: string }>;
  presence: Array<{ client_id: string; principal_id: string; display_name: string | null; selected_feature_id: string | null }>;
}
