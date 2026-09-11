import type { DocumentArtifact } from './document';

export interface ReleaseArtifact extends DocumentArtifact { filename: string }
export interface DocumentRelease {
  release_id:string; release_name:string; source_revision_id:string; source_state_version:number;
  created_at:string; status:string; error_code:string | null; error_message:string | null;
}
export interface ReleaseSource {
  document_id:string; project_id:string; revision_id:string; state_version:number; fcstd_artifact_id:string; fcstd_sha256:string;
}
export interface ReleaseManifest {
  schema_version:'cad-engineering-release.v1'; release_name:string; source:ReleaseSource;
  files:Array<{path:string;role:string;size_bytes:number;sha256:string}>;
  engineering_artifacts:Array<{workflow_run_id:string;artifact_kind:string;package_filename:string;sha256:string}>;
  bom:{native_type:string;instance_count:number;definition_count:number};
  units:'mm'; total_component_volume_mm3:number; mesh_triangles:number; scope:string;
}
export interface ReleaseResult {
  workflow_run_id:string; source_revision_id:string; source_state_version:number;
  report:ReleaseManifest & {kind:'release_package';source_fcstd_sha256:string};
  artifacts:Record<'engineering_report'|'engineering_bundle'|'release_manifest'|'release_bom_json'|'release_bom_csv'|'release_step'|'release_stl',ReleaseArtifact>;
}
export interface ReleaseBOM {
  schema_version:'cad-release-bom.v1'; source:ReleaseSource;
  generator:{native_type:string;freecad_version:string}; instance_count:number; definition_count:number;
  rows:Array<{index:string;name:string;quantity:number;kernel_name:string;definition_name:string;file_name:string}>;
}
