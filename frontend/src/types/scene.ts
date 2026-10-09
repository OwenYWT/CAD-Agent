export type SceneLOD = "coarse" | "medium" | "fine";
export interface SceneMesh {
  url: string; sha256: string; size_bytes: number; triangles: number;
  face_ranges?: { face_index: number; start: number; count: number }[];
  linear_deflection_mm: number; angular_deflection_rad: number;
}
export interface SceneInstance {
  kernel_name: string; label: string; feature_id: string; geometry_sha256: string;
  matrix: number[]; is_instance: boolean; face_bindings?: { face_index: number; axis: "x" | "y" | "z"; extreme: "min" | "max" }[];
}
export interface DocumentScene {
  schema_version: "cad-scene.v1"; document_id: string; revision_id: string; units: "mm";
  instances: SceneInstance[];
  definitions: Record<string, { bounds_mm: number[]; lods: Record<SceneLOD, SceneMesh> }>;
  generated_definitions: number; reused_definitions: number;
}
