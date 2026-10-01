import { authFetch } from "../../auth";
import type { ChangeSetActionResult, DurableChangeSetDetail } from "../../types/engineering";
import { API_BASE } from "./http";
import { readJson } from "./http";

export async function getDurableChangeSet(
  changeSetId: string,
): Promise<DurableChangeSetDetail> {
  const response = await authFetch(
    `${API_BASE}/api/change-sets/${encodeURIComponent(changeSetId)}`,
  );
  return readJson<DurableChangeSetDetail>(response, "变更审查加载失败");
}

async function changeSetAction(
  changeSetId: string,
  action: "accept" | "reject" | "request-change" | "commit" | "rollback",
  note?: string,
): Promise<ChangeSetActionResult> {
  const response = await authFetch(
    `${API_BASE}/api/change-sets/${encodeURIComponent(changeSetId)}/${action}`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: action === "commit" ? undefined : JSON.stringify({ note: note || "" }),
    },
  );
  return readJson<ChangeSetActionResult>(response, "变更操作失败");
}

export const acceptDurableChangeSet = (id: string, note = "") =>
  changeSetAction(id, "accept", note);

export const rejectDurableChangeSet = (id: string, note: string) =>
  changeSetAction(id, "reject", note);

export const requestDurableChangeSetModification = (id: string, note: string) =>
  changeSetAction(id, "request-change", note);

export const commitDurableChangeSet = (id: string) =>
  changeSetAction(id, "commit");

export const rollbackDurableChangeSet = (id: string, note = "") =>
  changeSetAction(id, "rollback", note);
