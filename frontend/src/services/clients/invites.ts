import { API_BASE, authFetch, readJson } from "./http";

export interface InviteCode {
  code: string;
  max_uses: number;
  used_count: number;
  expires_at: string | null;
  disabled_at: string | null;
  created_at: string;
}

const invites = `${API_BASE}/api/auth/invites`;

export async function listInvites(): Promise<InviteCode[]> {
  return readJson(await authFetch(invites), "邀请码加载失败");
}

export async function createInviteCode(code: string, maxUses: number): Promise<InviteCode> {
  return readJson(await authFetch(invites, {method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({code, max_uses: maxUses})}), "邀请码创建失败");
}

export async function disableInviteCode(code: string): Promise<void> {
  const response = await authFetch(`${invites}/${encodeURIComponent(code)}`, {method: "DELETE"});
  if (!response.ok) throw new Error("邀请码禁用失败");
}

export async function findInvite(code: string): Promise<InviteCode | null> {
  const response = await authFetch(`${invites}/${encodeURIComponent(code)}`);
  return response.status === 404 ? null : readJson(response, "邀请码结果核对失败");
}
