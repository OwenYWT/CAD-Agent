import { useState } from "react";
import type { AuthUser } from "../auth";
import { authFetch, deleteAuthAccount, refreshAuthSession } from "../auth";
import { Icon } from "./ui/Icon";

const API_BASE = import.meta.env.VITE_API_BASE || "";

interface InviteCode {
  code: string;
  max_uses: number;
  used_count: number;
  expires_at: string | null;
  disabled_at: string | null;
  created_at: string;
}

interface AccountPanelProps {
  user: AuthUser;
  onUserUpdate: (user: AuthUser) => void;
  onLogout: () => void;
}

export function AccountPanel({ user, onUserUpdate, onLogout }: AccountPanelProps) {
  const [open, setOpen] = useState(false);
  const [invites, setInvites] = useState<InviteCode[]>([]);
  const [newCode, setNewCode] = useState("");
  const [maxUses, setMaxUses] = useState(1);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const loadInvites = async () => {
    if (!user.is_admin) return;
    try {
      const res = await authFetch(`${API_BASE}/api/auth/invites`);
      if (!res.ok) throw new Error("邀请码加载失败");
      setInvites(await res.json());
    } catch (err) {
      setError(err instanceof Error ? err.message : "邀请码加载失败");
    }
  };

  const togglePanel = () => {
    const nextOpen = !open;
    setOpen(nextOpen);
    setError(null);
    if (nextOpen && user.is_admin) void loadInvites();
  };

  const createInvite = async () => {
    setError(null);
    setMessage(null);
    try {
      const res = await authFetch(`${API_BASE}/api/auth/invites`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ code: newCode.trim() || null, max_uses: maxUses }),
      });
      // Parse defensively: an error response may be empty or non-JSON, and reading
      // .detail off a failed parse would mask the real failure.
      const raw = await res.text();
      const data = raw ? (() => { try { return JSON.parse(raw); } catch { return null; } })() : null;
      if (!res.ok) throw new Error((data && data.detail) || "邀请码创建失败");
      if (!data || !data.code) throw new Error("邀请码创建失败");
      setMessage(`邀请码已创建：${data.code}`);
      setNewCode("");
      setMaxUses(1);
      await loadInvites();
    } catch (err) {
      setError(err instanceof Error ? err.message : "邀请码创建失败");
    }
  };

  const disableInvite = async (code: string) => {
    await authFetch(`${API_BASE}/api/auth/invites/${encodeURIComponent(code)}`, {
      method: "DELETE",
    });
    await loadInvites();
  };

  const deleteAccount = async () => {
    if (!window.confirm("确认注销当前账号？此操作不可恢复。")) return;
    try {
      await deleteAuthAccount();
      onLogout();
    } catch (err) {
      setError(err instanceof Error ? err.message : "注销账号失败");
    }
  };

  const refreshToken = async () => {
    setError(null);
    const session = await refreshAuthSession();
    if (session) {
      onUserUpdate(session.user);
      setMessage("登录状态已刷新");
    } else {
      setError("登录状态刷新失败，请重新登录");
    }
  };

  return (
    <div className="relative">
      <button
        aria-label="打开账号管理"
        onClick={togglePanel}
        className="icon-button text-slate-500 hover:bg-slate-100 hover:text-slate-900"
        title="账号管理"
        type="button"
      >
        <Icon name="user" size={18} />
      </button>

      {open && (
        <div className="fixed left-3 right-3 top-16 z-50 max-h-[78vh] space-y-4 overflow-y-auto rounded-md border border-slate-200 bg-white p-4 shadow-xl sm:absolute sm:left-auto sm:right-0 sm:top-11 sm:w-96">
          <div>
            <div className="text-sm font-semibold text-gray-800">账号管理</div>
            <div className="text-xs text-gray-500 mt-1">账号：{user.phone}</div>
          </div>

          <div className="grid grid-cols-2 gap-2">
            <button
              onClick={refreshToken}
              className="min-h-10 rounded-md bg-sky-50 text-sky-700 text-sm py-2 hover:bg-sky-100"
            >
              刷新登录状态
            </button>
            <button
              onClick={onLogout}
              className="min-h-10 rounded-md border border-gray-200 text-gray-700 text-sm py-2 hover:bg-gray-50"
            >
              退出登录
            </button>
          </div>

          {!user.is_admin && (
            <button
              onClick={deleteAccount}
              className="min-h-10 w-full rounded-md border border-red-200 bg-red-50 text-red-700 text-sm py-2 hover:bg-red-100"
            >
              注销账号
            </button>
          )}

          {user.is_admin && (
            <div className="border-t border-gray-100 pt-3 space-y-2">
              <div className="text-sm font-medium text-gray-700">邀请码管理</div>
              <div className="grid grid-cols-[minmax(0,1fr)_80px] gap-2 sm:grid-cols-[minmax(0,1fr)_80px_auto]">
                <input
                  value={newCode}
                  onChange={(event) => setNewCode(event.target.value)}
                  placeholder="留空自动生成"
                  aria-label="邀请码内容"
                  className="min-h-10 min-w-0 rounded-md border border-gray-200 px-3 py-2 text-xs"
                />
                <input
                  type="number"
                  min={1}
                  max={10000}
                  value={maxUses}
                  onChange={(event) => setMaxUses(Number(event.target.value))}
                  aria-label="邀请码最大使用次数"
                  className="min-h-10 w-20 rounded-md border border-gray-200 px-2 py-2 text-xs"
                />
                <button onClick={createInvite} className="min-h-10 rounded-md bg-gray-900 px-3 text-xs text-white max-sm:col-span-2">
                  创建
                </button>
              </div>

              <div className="space-y-1 max-h-52 overflow-y-auto">
                {invites.map((invite) => (
                  <div key={invite.code} className="flex items-center gap-2 rounded-lg bg-gray-50 px-2 py-1.5 text-xs">
                    <span className="font-mono flex-1 truncate">{invite.code}</span>
                    <span className="text-gray-400">{invite.used_count}/{invite.max_uses}</span>
                    {invite.disabled_at ? (
                      <span className="text-red-400">已禁用</span>
                    ) : (
                      <button onClick={() => disableInvite(invite.code)} className="text-red-500 hover:text-red-700">
                        禁用
                      </button>
                    )}
                  </div>
                ))}
                {invites.length === 0 && <div className="text-xs text-gray-400">暂无邀请码</div>}
              </div>
            </div>
          )}

          {message && <div className="rounded-lg bg-emerald-50 px-3 py-2 text-xs text-emerald-700">{message}</div>}
          {error && <div className="rounded-lg bg-red-50 px-3 py-2 text-xs text-red-700">{error}</div>}
        </div>
      )}
    </div>
  );
}
