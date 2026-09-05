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
  const snapshotRevision = snapshot.revision_id || snapshot.result.revision_id;
  if (result.revision_id && snapshotRevision) {
    return result.revision_id === snapshotRevision;
  }

  if (
    result.request_id
    && snapshot.result.request_id
  ) {
    return result.request_id === snapshot.result.request_id;
  }

  // Code is not identity: native edits can produce different states from the
  // same source. Only old records without durable identity may use this fallback.
  if (result.revision_id || snapshotRevision || result.request_id || snapshot.result.request_id) {
    return false;
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
