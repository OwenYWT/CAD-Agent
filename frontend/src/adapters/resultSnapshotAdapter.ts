import type {
  GenerationResult,
  ModelSnapshotDetail,
} from "../types";

function hasFiles(files: Record<string, string> | undefined): boolean {
  return Boolean(files && Object.keys(files).length > 0);
}

export function resultMatchesSnapshot(
  result: GenerationResult,
  snapshot: ModelSnapshotDetail,
): boolean {
  if (
    result.revision_id
    && snapshot.revision_id
    && result.revision_id === snapshot.revision_id
  ) {
    return true;
  }

  if (
    result.request_id
    && snapshot.result.request_id
    && result.request_id === snapshot.result.request_id
  ) {
    return true;
  }

  return Boolean(
    result.code
    && snapshot.code
    && result.code === snapshot.code,
  );
}

export function hydrateGenerationResult(
  result: GenerationResult,
  snapshot: ModelSnapshotDetail,
): GenerationResult {
  if (!resultMatchesSnapshot(result, snapshot)) return result;

  const snapshotResult = snapshot.result;
  return {
    ...snapshotResult,
    ...result,
    snapshot_id: result.snapshot_id || snapshot.id,
    version: result.version ?? snapshot.version,
    revision_id: result.revision_id || snapshot.revision_id,
    files: hasFiles(result.files)
      ? result.files
      : snapshot.files || snapshotResult.files,
    params: result.params || snapshot.params || snapshotResult.params,
    parameters:
      result.parameters
      || snapshot.parameters
      || snapshotResult.parameters,
    validation:
      result.validation
      || snapshot.validation
      || snapshotResult.validation,
    inspect_report:
      result.inspect_report
      || snapshot.inspect_report
      || snapshotResult.inspect_report,
    repair_history: result.repair_history?.length
      ? result.repair_history
      : snapshot.repair_history || snapshotResult.repair_history,
  };
}
