import type { GenerationResult } from "../types";
import { parseReferenceAttachmentsFromPrompt, type ReferenceAttachmentSummary } from "./referenceAttachments.ts";

export interface ArtifactFileEntry {
  format: string;
  url: string;
}

export interface ArtifactManifest {
  source: "CAD-Agent";
  adaptation_source: "forgecad-public-kit engineering artifact package";
  generated_at: string;
  request_id?: string;
  snapshot_id?: string;
  version?: number;
  prompt?: string;
  reference_attachments: ReferenceAttachmentSummary[];
  manufacturing_profile: GenerationResult["manufacturing_profile"] | null;
  available_files: ArtifactFileEntry[];
  code?: string;
  parameters: GenerationResult["parameters"] | null;
  params: GenerationResult["params"] | null;
  design_brief: GenerationResult["design_brief"];
  plan: GenerationResult["plan"];
  validation: GenerationResult["validation"];
  inspect_report: GenerationResult["inspect_report"];
  repair_history: GenerationResult["repair_history"];
  recovery_actions: GenerationResult["recovery_actions"];
}

interface BuildArtifactManifestOptions {
  prompt?: string | null;
  generatedAt?: string;
}

export function buildArtifactManifest(
  result: GenerationResult,
  options: BuildArtifactManifestOptions = {},
): ArtifactManifest {
  return {
    source: "CAD-Agent",
    adaptation_source: "forgecad-public-kit engineering artifact package",
    generated_at: options.generatedAt || new Date().toISOString(),
    request_id: result.request_id,
    snapshot_id: result.snapshot_id,
    version: result.version,
    prompt: options.prompt || undefined,
    reference_attachments: parseReferenceAttachmentsFromPrompt(options.prompt),
    manufacturing_profile: result.manufacturing_profile || null,
    available_files: Object.entries(result.files || {})
      .filter(([, url]) => Boolean(url))
      .map(([format, url]) => ({ format, url })),
    code: result.code,
    parameters: result.parameters || null,
    params: result.params || null,
    design_brief: result.design_brief || result.plan?.design_brief || null,
    plan: result.plan || null,
    validation: result.validation,
    inspect_report: result.inspect_report || null,
    repair_history: result.repair_history || [],
    recovery_actions: result.recovery_actions || [],
  };
}

export function manifestFilename(requestId?: string | null) {
  return `cad-agent-manifest-${requestId || "latest"}.json`;
}

export function manifestJson(manifest: ArtifactManifest) {
  return `${JSON.stringify(manifest, null, 2)}
`;
}
