export type SketchDimensionType = "DistanceX" | "DistanceY" | "Distance" | "Radius" | "Diameter" | "Angle";
export type SketchPoint = [number,number,number];
export interface SketchConstraint {
  index: number; type: string; name: string; value: number; driving?: boolean;
  first: number; first_position: number; second: number; second_position: number;
}
export interface SketchGeometry {
  index: number; type: string; center?: SketchPoint; start?: SketchPoint; end?: SketchPoint;
  radius_mm?: number; construction?: boolean;
}
export interface SketchDetails {
  geometry: SketchGeometry[]; constraints: SketchConstraint[];
  omitted_geometry: number; omitted_constraints: number;
}
export interface SketchDimensionEdit { constraint_index: number; expected_type: SketchDimensionType; value_mm?: number; value_deg?: number }
