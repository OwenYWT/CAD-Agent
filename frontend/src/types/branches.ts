export interface DocumentBranches {
  tenant_id: string; lineage_id: string;
  branches: Array<{ document_id: string; name: string; head_revision_id: string; state_version: number;
    workflow_run_id: string | null; preparation_status: string | null }>;
}

export interface SemanticChange {
  feature_id: string; kernel_name: string; label: string; kind: "added" | "removed" | "changed";
  fields: Array<{ name: string; before: unknown; after: unknown }>;
}

export interface BranchComparison {
  document_id: string; source_document_id: string; source_revision_id: string; source_state_version: number;
  target_revision_id: string; target_state_version: number; common_revision_id: string; comparison_hash: string;
  source_changes: SemanticChange[]; target_changes: SemanticChange[]; differences: SemanticChange[];
  can_merge_parameters: boolean; parameters: { updates: Array<{ parameter_id: string; value: number }>; reason: string | null };
  conflicts: Array<{ code: string; message: string }>; annotation_policy: string; source_geometry_policy: string;
}
