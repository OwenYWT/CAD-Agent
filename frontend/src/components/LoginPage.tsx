import { useMemo, useState } from "react";
import type { AuthSession } from "../auth";
import { saveAuthSession } from "../auth";

const API_BASE = import.meta.env.VITE_API_BASE || "";

type Screen = "login" | "register" | "reset";
type LoginMode = "password" | "code";
type RegisterMode = "code" | "invite";

interface LoginPageProps {
  onLogin: (session: AuthSession) => void;
}

async function readJsonResponse(res: Response) {
  const text = await res.text();
  if (!text) return null;
  try {
    return JSON.parse(text);
  } catch {
    return { detail: text };
  }
}

function errorMessage(data: unknown, fallback: string) {
  if (!data || typeof data !== "object") return fallback;
  const detail = (data as { detail?: unknown }).detail;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    return detail
      .map((item) => {
        if (item && typeof item === "object" && "msg" in item) {
          return String((item as { msg: unknown }).msg);
        }
        return String(item);
      })
      .join(" / ");
  }
  if (detail) return JSON.stringify(detail);
  return fallback;
}

function isPhoneReady(phone: string) {
  return phone.replace(/[\s-]/g, "").length >= 6;
}

function isAdminAccount(phone: string) {
  return phone.trim().toLowerCase() === "admin";
}

export function LoginPage({ onLogin }: LoginPageProps) {
  const [screen, setScreen] = useState<Screen>("login");
  const [loginMode, setLoginMode] = useState<LoginMode>("password");
  const [registerMode, setRegisterMode] = useState<RegisterMode>("code");
  const [phone, setPhone] = useState("");
  const [password, setPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [code, setCode] = useState("");
  const [inviteCode, setInviteCode] = useState("");
  const [devCode, setDevCode] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const purpose = screen === "register" ? "register" : screen === "reset" ? "reset_password" : "login";
  const needsCode = screen === "reset" || (screen === "register" ? registerMode === "code" : loginMode === "code");
  const passwordMismatch = (screen === "register" || screen === "reset") && password && confirmPassword && password !== confirmPassword;
  const canSubmit = useMemo(() => {
    if (loading) return false;
    if (screen === "login") {
      if (isAdminAccount(phone)) return loginMode === "password" && password.length > 0;
      if (!isPhoneReady(phone)) return false;
      return loginMode === "password" ? password.length > 0 : code.trim().length > 0;
    }
    if (!isPhoneReady(phone) || isAdminAccount(phone)) return false;
    if (password.length < 8 || password !== confirmPassword) return false;
    if (screen === "reset") return code.trim().length > 0;
    return registerMode === "code" ? code.trim().length > 0 : inviteCode.trim().length > 0;
  }, [phone, loading, screen, loginMode, password, code, confirmPassword, registerMode, inviteCode]);

  const resetTransient = () => {
    setError(null);
    setMessage(null);
    setDevCode(null);
    setCode("");
  };

  const requestCode = async () => {
    setLoading(true);
    setError(null);
    setMessage(null);
    try {
      const res = await fetch(`${API_BASE}/api/auth/code/request`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ phone, purpose }),
      });
      const data = await readJsonResponse(res);
      if (!res.ok) throw new Error(errorMessage(data, "???????"));
      const payload = data as { dev_code?: string; message?: string } | null;
      setDevCode(payload?.dev_code || null);
      setMessage(payload?.message || "??????");
    } catch (err) {
      setError(err instanceof Error ? err.message : "???????");
    } finally {
      setLoading(false);
    }
  };

  const submit = async () => {
    setLoading(true);
    setError(null);
    try {
      let endpoint = "";
      let body: Record<string, string> = { phone };
      if (screen === "register" && registerMode === "code") {
        endpoint = "/api/auth/register/code";
        body = { phone, code, password };
      } else if (screen === "register" && registerMode === "invite") {
        endpoint = "/api/auth/register/invite";
        body = { phone, invite_code: inviteCode, password };
      } else if (screen === "reset") {
        endpoint = "/api/auth/password/reset";
        body = { phone, code, password };
      } else if (screen === "login" && loginMode === "password") {
        endpoint = "/api/auth/login/password";
        body = { phone, password };
      } else {
        endpoint = "/api/auth/login/code";
        body = { phone, code };
      }

      const res = await fetch(`${API_BASE}${endpoint}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const data = await readJsonResponse(res);
      if (!res.ok) throw new Error(errorMessage(data, "????"));
      if (!data) throw new Error("???????");
      saveAuthSession(data as AuthSession);
      onLogin(data as AuthSession);
    } catch (err) {
      setError(err instanceof Error ? err.message : "????");
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="min-h-screen bg-gradient-to-br from-slate-950 via-indigo-950 to-slate-900 flex items-center justify-center p-6">
      <div className="w-full max-w-md bg-white/95 backdrop-blur rounded-2xl shadow-2xl border border-white/20 p-8 space-y-6">
        <div className="space-y-2 text-center">
          <div className="text-3xl font-bold text-slate-900">CAD Agent</div>
          <p className="text-sm text-slate-500">注册或登录后开始生成、预览和管理 CAD 模型</p>
        </div>

        <div className="grid grid-cols-3 gap-2 rounded-xl bg-slate-100 p-1">
          <button
            type="button"
            onClick={() => { setScreen("login"); resetTransient(); }}
            className={`rounded-lg py-2 text-sm font-medium transition ${screen === "login" ? "bg-white text-indigo-700 shadow" : "text-slate-500 hover:text-slate-700"}`}
          >
            登录
          </button>
          <button
            type="button"
            onClick={() => { setScreen("register"); resetTransient(); }}
            className={`rounded-lg py-2 text-sm font-medium transition ${screen === "register" ? "bg-white text-indigo-700 shadow" : "text-slate-500 hover:text-slate-700"}`}
          >
            注册
          </button>
        </div>

        {screen === "reset" ? (
          <div className="rounded-xl bg-amber-50 border border-amber-100 px-3 py-2 text-xs text-amber-700">
            ????????????????????????
          </div>
        ) : screen === "login" ? (
          <div className="grid grid-cols-2 gap-2 rounded-xl bg-slate-50 p-1">
            <button type="button" onClick={() => { setLoginMode("password"); resetTransient(); }} className={`rounded-lg py-2 text-xs font-medium ${loginMode === "password" ? "bg-white text-slate-900 shadow" : "text-slate-500"}`}>密码登录</button>
            <button type="button" onClick={() => { setLoginMode("code"); resetTransient(); }} className={`rounded-lg py-2 text-xs font-medium ${loginMode === "code" ? "bg-white text-slate-900 shadow" : "text-slate-500"}`}>验证码登录</button>
          </div>
        ) : (
          <div className="grid grid-cols-2 gap-2 rounded-xl bg-slate-50 p-1">
            <button type="button" onClick={() => { setRegisterMode("code"); resetTransient(); }} className={`rounded-lg py-2 text-xs font-medium ${registerMode === "code" ? "bg-white text-slate-900 shadow" : "text-slate-500"}`}>验证码注册</button>
            <button type="button" onClick={() => { setRegisterMode("invite"); resetTransient(); }} className={`rounded-lg py-2 text-xs font-medium ${registerMode === "invite" ? "bg-white text-slate-900 shadow" : "text-slate-500"}`}>邀请码注册</button>
          </div>
        )}

        <div className="space-y-4">
          <label className="block space-y-1.5">
            <span className="text-sm font-medium text-slate-700">手机号</span>
            <input
              value={phone}
              onChange={(e) => setPhone(e.target.value)}
              placeholder="请输入手机号"
              className="w-full rounded-xl border border-slate-200 px-4 py-3 text-sm outline-none focus:border-indigo-500 focus:ring-2 focus:ring-indigo-100"
            />
          </label>

          {screen === "login" && loginMode === "password" && (
            <label className="block space-y-1.5">
              <span className="text-sm font-medium text-slate-700">密码</span>
              <input type="password" value={password} onChange={(e) => setPassword(e.target.value)} placeholder="请输入密码" className="w-full rounded-xl border border-slate-200 px-4 py-3 text-sm outline-none focus:border-indigo-500 focus:ring-2 focus:ring-indigo-100" />
            </label>
          )}

          {(screen === "register" || screen === "reset") && (
            <>
              <label className="block space-y-1.5">
                <span className="text-sm font-medium text-slate-700">设置密码</span>
                <input type="password" value={password} onChange={(e) => setPassword(e.target.value)} placeholder="至少 8 位" className="w-full rounded-xl border border-slate-200 px-4 py-3 text-sm outline-none focus:border-indigo-500 focus:ring-2 focus:ring-indigo-100" />
              </label>
              <label className="block space-y-1.5">
                <span className="text-sm font-medium text-slate-700">确认密码</span>
                <input type="password" value={confirmPassword} onChange={(e) => setConfirmPassword(e.target.value)} placeholder="再次输入密码" className="w-full rounded-xl border border-slate-200 px-4 py-3 text-sm outline-none focus:border-indigo-500 focus:ring-2 focus:ring-indigo-100" />
                {passwordMismatch && <span className="text-xs text-red-500">两次输入的密码不一致</span>}
              </label>
            </>
          )}

          {needsCode && (
            <div className="space-y-2">
              <div className="flex gap-2">
                <input value={code} onChange={(e) => setCode(e.target.value)} placeholder="请输入验证码" className="min-w-0 flex-1 rounded-xl border border-slate-200 px-4 py-3 text-sm outline-none focus:border-indigo-500 focus:ring-2 focus:ring-indigo-100" />
                <button type="button" onClick={requestCode} disabled={loading || !isPhoneReady(phone)} className="rounded-xl bg-slate-900 px-4 py-3 text-sm font-medium text-white disabled:cursor-not-allowed disabled:opacity-50">获取验证码</button>
              </div>
              {devCode && <div className="rounded-lg bg-amber-50 border border-amber-200 px-3 py-2 text-xs text-amber-700">开发模式验证码：<span className="font-mono font-semibold">{devCode}</span></div>}
            </div>
          )}

          {screen === "register" && registerMode === "invite" && (
            <label className="block space-y-1.5">
              <span className="text-sm font-medium text-slate-700">邀请码</span>
              <input value={inviteCode} onChange={(e) => setInviteCode(e.target.value)} placeholder="请输入开发者提供的邀请码" className="w-full rounded-xl border border-slate-200 px-4 py-3 text-sm outline-none focus:border-indigo-500 focus:ring-2 focus:ring-indigo-100" />
              <span className="text-xs text-slate-400">本地默认邀请码：CAD-AGENT-2026</span>
            </label>
          )}
        </div>

        {message && <div className="rounded-lg bg-emerald-50 px-3 py-2 text-sm text-emerald-700">{message}</div>}
        {error && <div className="rounded-lg bg-red-50 px-3 py-2 text-sm text-red-700">{error}</div>}

        <button type="button" onClick={submit} disabled={!canSubmit} className="w-full rounded-xl bg-indigo-600 py-3 text-sm font-semibold text-white shadow-lg shadow-indigo-600/20 transition hover:bg-indigo-700 disabled:cursor-not-allowed disabled:opacity-50">
          {loading ? "处理中..." : screen === "register" ? "注册并登录" : "登录"}
        </button>
      </div>
    </div>
  );
}
