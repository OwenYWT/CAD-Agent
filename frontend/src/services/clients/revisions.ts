import { authFetch } from "../../auth";
import type { FreeCADBOMDocument, ModelSnapshotDiff, ModelSnapshotDetail, ModelSnapshotSummary } from "../../types";
import { API_BASE } from "./http";
import { readJson } from "./http";

export async function getRevisionBOM(
  projectId: string,
  revisionId: string,
  signal?: AbortSignal,
): Promise<FreeCADBOMDocument> {
  const response = await authFetch(
    `${API_BASE}/api/projects/${encodeURIComponent(projectId)}`
      + `/revisions/${encodeURIComponent(revisionId)}/bom`,
    { signal },
  );
  return readJson<FreeCADBOMDocument>(response, "BOM 加载失败");
}

export async function listModelSnapshots(
  panelId: string,
): Promise<ModelSnapshotSummary[]> {
  const response = await authFetch(
    `${API_BASE}/api/history/panels/${encodeURIComponent(panelId)}/snapshots`,
  );
  return readJson<ModelSnapshotSummary[]>(response, "版本列表加载失败");
}

export async function getModelSnapshot(
  snapshotId: string,
): Promise<ModelSnapshotDetail> {
  const response = await authFetch(
    `${API_BASE}/api/history/snapshots/${encodeURIComponent(snapshotId)}`,
  );
  return readJson<ModelSnapshotDetail>(response, "版本证据加载失败");
}

export async function diffModelSnapshots(
  fromSnapshotId: string,
  toSnapshotId: string,
): Promise<ModelSnapshotDiff> {
  const response = await authFetch(
    `${API_BASE}/api/history/snapshots/${encodeURIComponent(fromSnapshotId)}/diff/${encodeURIComponent(toSnapshotId)}`,
  );
  return readJson<ModelSnapshotDiff>(response, "版本对比加载失败");
}
