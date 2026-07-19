import type { ManufacturingProfile } from "../types";

export interface ManufacturingProfilePreset {
  id: string;
  label: string;
  description: string;
  profile: ManufacturingProfile;
}

export const DEFAULT_MANUFACTURING_PROFILE: ManufacturingProfile = {
  process: "fdm",
  material: "PLA",
  nozzle_diameter_mm: 0.4,
  layer_height_mm: 0.2,
  build_volume_mm: [220, 220, 250],
};

export const MANUFACTURING_PROFILE_PRESETS: ManufacturingProfilePreset[] = [
  {
    id: "fdm-pla",
    label: "FDM PLA",
    description: "\u901a\u7528\u684c\u9762 FDM 3D \u6253\u5370",
    profile: DEFAULT_MANUFACTURING_PROFILE,
  },
  {
    id: "fdm-petg",
    label: "FDM PETG",
    description: "\u66f4\u9002\u5408\u529f\u80fd\u4ef6\u548c\u8010\u7528\u4ef6",
    profile: {
      process: "fdm",
      material: "PETG",
      nozzle_diameter_mm: 0.4,
      layer_height_mm: 0.2,
      build_volume_mm: [220, 220, 250],
    },
  },
  {
    id: "sla-resin",
    label: "SLA Resin",
    description: "\u9002\u5408\u9ad8\u7ec6\u8282\u6811\u8102\u6253\u5370",
    profile: {
      process: "sla",
      material: "Resin",
      nozzle_diameter_mm: null,
      layer_height_mm: 0.05,
      build_volume_mm: [120, 68, 150],
    },
  },
  {
    id: "generic",
    label: "Generic",
    description: "\u9002\u5408\u9ad8\u7ec6\u8282\u6811\u8102\u6253\u5370",
    profile: {
      process: "generic",
      material: "Generic",
      nozzle_diameter_mm: null,
      layer_height_mm: null,
      build_volume_mm: [220, 220, 250],
    },
  },
];

export function getManufacturingProfilePreset(id: string) {
  return MANUFACTURING_PROFILE_PRESETS.find((preset) => preset.id === id) || MANUFACTURING_PROFILE_PRESETS[0];
}

export function formatManufacturingProfile(profile: ManufacturingProfile) {
  const process = profile.process === "laser_cut" ? "\u6fc0\u5149\u5207\u5272" : profile.process.toUpperCase();
  const parts = [`${process} ${profile.material}`.trim()];
  if (profile.nozzle_diameter_mm != null) parts.push(`\u55b7\u5634 ${profile.nozzle_diameter_mm}mm`);
  if (profile.layer_height_mm != null) parts.push(`\u5c42\u9ad8 ${profile.layer_height_mm}mm`);
  if (profile.build_volume_mm?.length) parts.push(`${profile.build_volume_mm.join("x")}mm`);
  return parts.join(" - ");
}
