/** User-supplied basis; references are never treated as independently verified measurements. */
export interface RequirementBasis {
  schema_version: "requirement-basis.v1";
  target: string;
  purpose: string;
  design_scope?: "geometry" | "physical_fit";
  source_kind: "none" | "user_specification" | "user_measurement" | "reference";
  source_reference: string;
  dimensions: string;
  fit_notes: string;
  concept_acknowledged: boolean;
}
