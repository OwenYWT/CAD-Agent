import type { DocumentArtifact } from './document';

export interface BoundaryPlane { axis: 'x' | 'y' | 'z'; side: 'min' | 'max' }
export interface LinearStaticTask {
  kind: 'linear_static'; component_name: string;
  material: { name: string; young_modulus_mpa: number; poisson_ratio: number };
  mesh_size_mm: number; fixed_face: BoundaryPlane; loaded_face: BoundaryPlane; force_n: [number, number, number];
}
export interface ContourMillingTask {
  kind:'contour_milling';component_name:string;tool:{name:string;diameter_mm:number;cutting_length_mm:number};
  postprocessor:'grbl_1_1';work_origin_mm:[number,number,number];stepdown_mm:number;feed_mm_min:number;plunge_mm_min:number;
  spindle_rpm:number;safe_height_mm:number;stock_margin_mm:number;radial_allowance_mm:number;chord_tolerance_mm:number;
}
export type EngineeringTask=LinearStaticTask | ContourMillingTask;
export interface EngineeringTaskSummary {
  workflow_run_id: string; source_revision_id: string; source_state_version: number; task_kind: string;
  created_at: string; status: string; error_code: string | null; error_message: string | null; task: EngineeringTask;
}
export interface CamReport extends ContourMillingTask {
  source_fcstd_sha256:string;scope:string;passes:number;segments:number;depths_mm:number[];minimum_target_clearance_mm:number;
  feed_path_length_mm:number;rapid_path_length_mm:number;feed_time_seconds:number;internal_loops_not_machined:number;
}
export interface EngineeringResult {
  workflow_run_id: string; source_revision_id: string; source_state_version: number;
  report: CamReport | { kind: 'linear_static'; component_name: string; source_fcstd_sha256: string; scope: string; nodes: number; elements: number;
    maximum: { displacement_mm: number; von_mises_mpa: number }; force_balance_relative_error: number;
    solver: { name: string; version: string }; mesher: { name: string; version: string }; material: LinearStaticTask['material'];
    force_n: number[]; reaction_n: number[]; fixed_face: BoundaryPlane; loaded_face: BoundaryPlane; mesh_size_mm: number };
  artifacts: Record<'engineering_report' | 'engineering_field' | 'engineering_bundle', DocumentArtifact & { filename: string }>
    & {cam_program?:DocumentArtifact & {filename:string}};
}
export interface CamToolpath {
  schema_version:'cad-cam-toolpath.v1';units:'mm';postprocessor:'grbl_1_1';work_origin_mm:number[];
  positions_mm:number[][];triangles:number[][];safe_z_mm:number;cutter_diameter_mm:number;
  trajectory:Array<{motion:'rapid' | 'feed';position_mm:[number,number,number];feed_mm_min:number | null}>;
}
export interface EngineeringField {
  schema_version: 'cad-fea-field.v1'; positions_mm: number[][]; displacements_mm: number[][];
  von_mises_mpa: number[]; triangles: number[][];
}
