import { useEffect, useRef, useState } from "react";
import type { AuthUser } from "../auth";
import { authFetch, deleteAuthAccount, refreshAuthSession } from "../auth";
import { useI18n } from "../i18n/I18nContext";
import { Icon } from "./ui/Icon";
import { useMutationReceipt } from '../hooks/useMutationReceipt';
import { readJson } from '../services/clients/http';

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
  const { translate } = useI18n();
  const [open, setOpen] = useState(false);
  const [invites, setInvites] = useState<InviteCode[]>([]);
  const [newCode, setNewCode] = useState("");
  const [maxUses, setMaxUses] = useState(1);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [invitesLoading, setInvitesLoading] = useState(false);
  const [pending, setPending] = useState("");
  const lock = useRef(false);
  const [uncertainMutation, setUncertainMutation] = useMutationReceipt<{ action: 'create' | 'disable'; code: string; maxUses?: number }>(`invites:${user.id}`);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const panelRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        setOpen(false);
        window.requestAnimationFrame(() => triggerRef.current?.focus());
      }
    };
    const onPointerDown = (event: PointerEvent) => {
      const target = event.target;
      if (!(target instanceof Node)) return;
      if (panelRef.current?.contains(target) || triggerRef.current?.contains(target)) return;
      setOpen(false);
    };
    window.addEventListener("keydown", onKeyDown);
    document.addEventListener("pointerdown", onPointerDown);
    return () => {
      window.removeEventListener("keydown", onKeyDown);
      document.removeEventListener("pointerdown", onPointerDown);
    };
  }, [open]);

  const closePanel = () => {
    setOpen(false);
    window.requestAnimationFrame(() => triggerRef.current?.focus());
  };

  const loadInvites = async () => {
    if (!user.is_admin) return;
    setInvitesLoading(true);
    try {
      const res = await authFetch(`${API_BASE}/api/auth/invites`);
      if (!res.ok) throw new Error("邀请码加载失败");
      const data = await res.json();
      if (!Array.isArray(data)) throw new Error('邀请码列表格式无效');
      setInvites(data);
    } catch (err) {
      setError(err instanceof Error ? err.message : "邀请码加载失败");
    } finally { setInvitesLoading(false); }
  };

  const togglePanel = () => {
    const nextOpen = !open;
    setOpen(nextOpen);
    setError(null);
    if (nextOpen && user.is_admin) void loadInvites();
  };

  const createInvite = async () => {
    if (lock.current || uncertainMutation) return;
    if (!Number.isInteger(maxUses) || maxUses < 1 || maxUses > 10000) { setError("使用次数必须为1到10000的整数"); return; }
    lock.current = true; setPending("create");
    setError(null);
    setMessage(null);
    const receipt = { action: 'create' as const, code: (newCode.trim() || crypto.randomUUID().replace(/-/g, '').slice(0, 16)).toUpperCase(), maxUses };
    setUncertainMutation(receipt);
    let failure = '';
    try {
      const res = await authFetch(`${API_BASE}/api/auth/invites`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ code: receipt.code, max_uses: maxUses }),
      });
      // Parse defensively: an error response may be empty or non-JSON, and reading
      // .detail off a failed parse would mask the real failure.
      const raw = await res.text();
      const data = raw ? (() => { try { return JSON.parse(raw); } catch { return null; } })() : null;
      if (!res.ok) throw new Error((data && data.detail) || "邀请码创建失败");
      if (!data || !data.code) throw new Error("邀请码创建失败");
    } catch (err) {
      failure = err instanceof Error ? err.message : "邀请码创建失败";
    }
    try { await reconcileInvite(receipt, failure); }
    finally { lock.current = false; setPending(""); }
  };

  const disableInvite = async (code: string) => {
    if (lock.current || uncertainMutation) return;
    lock.current = true; setPending(code); setError(null); setMessage(null);
    const receipt = { action: 'disable' as const, code }; setUncertainMutation(receipt);
    let failure = '';
    try {
      const response = await authFetch(`${API_BASE}/api/auth/invites/${encodeURIComponent(code)}`, { method: "DELETE" });
      if (!response.ok) throw new Error("邀请码禁用失败");
    } catch (reason) { failure = reason instanceof Error ? reason.message : "邀请码禁用失败"; }
    try { await reconcileInvite(receipt, failure); }
    finally { lock.current = false; setPending(""); }
  };

  const reconcileInvite = async (receipt: NonNullable<typeof uncertainMutation>, failure = '') => {
    setPending('verify'); setError(null);
    try {
      const response = await authFetch(`${API_BASE}/api/auth/invites/${encodeURIComponent(receipt.code)}`);
      const actual = response.status === 404 ? null : await readJson<InviteCode>(response, '邀请码结果核对失败');
      if (actual && actual.code !== receipt.code) throw new Error('邀请码结果身份不匹配');
      const applied = receipt.action === 'disable' ? !actual || Boolean(actual.disabled_at) : Boolean(actual && !actual.disabled_at && actual.max_uses === receipt.maxUses);
      setInvites(previous => actual ? [actual, ...previous.filter(item => item.code !== receipt.code)] : previous.filter(item => item.code !== receipt.code));
      setUncertainMutation(null);
      if (applied) {
        setMessage(receipt.action === 'disable' ? '已核对，邀请码已不可用。' : '已核对，邀请码已可用。');
        if (receipt.action === 'create') { setNewCode(''); setMaxUses(1); }
      } else { setError(`${failure ? failure + '；' : ''}已核对服务器，操作目标尚未生效，可以重试。`); }
      await loadInvites();
    } catch { setError('操作结果未确认，请恢复连接后核对结果。'); }
    finally { setPending(''); }
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
        aria-controls="account-management-panel"
        aria-expanded={open}
        onClick={togglePanel}
        className="workspace-icon-button"
        ref={triggerRef}
        title="账号管理"
        type="button"
      >
        <Icon name="user" size={18} />
      </button>

      {open && (
        <div aria-label="账号管理" className="ww-account-panel" id="account-management-panel" ref={panelRef} role="dialog">
          <div className="ww-account-header">
            <div>
              <div className="type-section-heading text-[var(--ink)]">账号管理</div>
              <div className="mt-1 type-caption text-[var(--faint)]">账号：{user.registered_via === "auth_disabled" ? <span>{translate(user.phone)}</span> : <span data-i18n-skip>{user.phone}</span>}</div>
            </div>
            <button aria-label="关闭账号管理" className="workspace-icon-button" onClick={closePanel} type="button"><Icon name="x" size={16} /></button>
          </div>

          <div className="grid grid-cols-2 gap-2">
            <button
              disabled={Boolean(pending)}
              onClick={refreshToken}
              className="workspace-button min-h-11"
              type="button"
            >
              刷新登录状态
            </button>
            <button
              disabled={Boolean(pending)}
              onClick={onLogout}
              className="workspace-button min-h-11"
              type="button"
            >
              退出登录
            </button>
          </div>

          {!user.is_admin && (
            <button
              onClick={deleteAccount}
              className="min-h-11 w-full rounded-md border border-red-200 bg-red-50 text-red-700 type-control py-2 hover:bg-red-100"
              type="button"
            >
              注销账号
            </button>
          )}

          {user.is_admin && (
            <div className="space-y-2 border-t border-[var(--line)] pt-3">
              <div className="type-section-heading text-[var(--ink)]">邀请码管理</div>
              <div className="grid grid-cols-[minmax(0,1fr)_80px] gap-2 sm:grid-cols-[minmax(0,1fr)_80px_auto]">
                <input
                  value={newCode}
                  disabled={Boolean(pending) || Boolean(uncertainMutation)}
                  onChange={(event) => setNewCode(event.target.value)}
                  placeholder="留空自动生成"
                  aria-label="邀请码内容"
                  className="min-h-11 min-w-0 rounded-md border border-[var(--line-strong)] bg-[var(--surface)] px-3 py-2 type-control"
                />
                <input
                  type="number"
                  min={1}
                  max={10000}
                  value={maxUses}
                  disabled={Boolean(pending) || Boolean(uncertainMutation)}
                  onChange={(event) => setMaxUses(Number(event.target.value))}
                  aria-label="邀请码最大使用次数"
                  className="min-h-11 w-20 rounded-md border border-[var(--line-strong)] bg-[var(--surface)] px-2 py-2 type-control"
                />
                <button disabled={Boolean(pending) || Boolean(uncertainMutation) || invitesLoading} onClick={createInvite} className="workspace-button workspace-button--primary min-h-11 max-sm:col-span-2" type="button">
                  {pending === "create" ? "正在创建" : "创建"}
                </button>
              </div>

              {invitesLoading ? <p role="status">正在加载邀请码…</p> : null}
              {uncertainMutation ? <div role="status"><p>操作结果未确认，请核对后继续。</p><button className="workspace-button" type="button" disabled={Boolean(pending)} onClick={() => void reconcileInvite(uncertainMutation)}>核对结果</button></div> : null}
              <div className="space-y-1 max-h-52 overflow-y-auto">
                {invites.map((invite) => (
                  <div key={invite.code} className="flex items-center gap-2 rounded-md bg-[var(--subtle)] px-2 py-1.5 type-body">
                    <span className="flex-1 truncate font-mono" data-i18n-skip>{invite.code}</span>
                    <span className="text-[var(--faint)]">{invite.used_count}/{invite.max_uses}</span>
                    {invite.disabled_at ? (
                      <span className="text-red-400">已禁用</span>
                    ) : (
                      <button disabled={Boolean(pending) || Boolean(uncertainMutation)} onClick={() => void disableInvite(invite.code)} className="text-red-500 hover:text-red-700" type="button">
                        {pending === invite.code ? "正在禁用" : "禁用"}
                      </button>
                    )}
                  </div>
                ))}
                {!invitesLoading && !error && invites.length === 0 && <div className="type-body text-[var(--faint)]">暂无邀请码</div>}
              </div>
            </div>
          )}

          {message && <div className="rounded-md bg-emerald-50 px-3 py-2 type-body text-emerald-700">{message}</div>}
          {error && <div role="alert" className="rounded-md bg-red-50 px-3 py-2 type-body text-red-700" data-i18n-skip>{error}</div>}
        </div>
      )}
    </div>
  );
}
