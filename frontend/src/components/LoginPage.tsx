import { useMemo, useState } from "react";
import type { AuthSession } from "../auth";
import { saveAuthSession } from "../auth";
import { BrandMark } from "./common/BrandMark";
import { Icon } from "./ui/Icon";

const API_BASE = import.meta.env.VITE_API_BASE || "";
type Screen = "login" | "register";

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
    return detail.map((item) => item && typeof item === "object" && "msg" in item ? String((item as { msg: unknown }).msg) : String(item)).join(" / ");
  }
  return detail ? JSON.stringify(detail) : fallback;
}

function isPhoneReady(phone: string) {
  return phone.replace(/[\s-]/g, "").length >= 6;
}

function isAdminAccount(phone: string) {
  return phone.trim().toLowerCase() === "admin";
}

export function LoginPage({ onLogin }: LoginPageProps) {
  const [screen, setScreen] = useState<Screen>("login");
  const [phone, setPhone] = useState("");
  const [password, setPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [inviteCode, setInviteCode] = useState("");
  const [showPassword, setShowPassword] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const passwordMismatch = screen === "register" && Boolean(password && confirmPassword && password !== confirmPassword);
  const canSubmit = useMemo(() => {
    if (loading) return false;
    if (screen === "login") return (isAdminAccount(phone) || isPhoneReady(phone)) && password.length > 0;
    return isPhoneReady(phone) && !isAdminAccount(phone) && password.length >= 8 && password === confirmPassword && inviteCode.trim().length > 0;
  }, [confirmPassword, inviteCode, loading, password, phone, screen]);

  const switchScreen = (nextScreen: Screen) => {
    setScreen(nextScreen);
    setError(null);
  };

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!canSubmit) return;
    setLoading(true);
    setError(null);
    try {
      const endpoint = screen === "register" ? "/api/auth/register/invite" : "/api/auth/login/password";
      const body = screen === "register"
        ? { phone: phone.trim(), invite_code: inviteCode.trim(), password }
        : { phone: phone.trim(), password };
      const res = await fetch(`${API_BASE}${endpoint}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const data = await readJsonResponse(res);
      if (!res.ok) throw new Error(errorMessage(data, "操作失败，请稍后重试"));
      if (!data) throw new Error("服务器无响应，请稍后重试");
      saveAuthSession(data as AuthSession);
      onLogin(data as AuthSession);
    } catch (err) {
      setError(err instanceof Error ? err.message : "操作失败，请稍后重试");
    } finally {
      setLoading(false);
    }
  };

  return (
    <main className="flex min-h-[100dvh] items-center justify-center bg-slate-950 p-4 sm:p-6">
      <div className="w-full max-w-[420px]">
        <BrandMark
          className="mb-6 w-full justify-center text-white"
          description="参数化建模工作台"
          descriptionClassName="text-sm text-slate-400"
          nameAs="h1"
          nameClassName="text-[22px]"
          size="login"
          tone="inverse"
        />

        <form className="space-y-5 rounded-lg border border-slate-200 bg-white p-5 shadow-2xl sm:p-7" onSubmit={submit}>
          <div>
            <h2 className="text-lg font-semibold text-slate-900">进入工作台</h2>
            <p className="mt-1 text-sm text-slate-500">登录后生成、检查并导出 CAD 模型。</p>
          </div>

          <div aria-label="账号操作" className="grid grid-cols-2 rounded-md bg-slate-100 p-1" role="tablist">
            <button aria-selected={screen === "login"} className={`min-h-10 rounded-[4px] text-sm font-medium ${screen === "login" ? "bg-white text-sky-700 shadow-sm" : "text-slate-600 hover:text-slate-900"}`} onClick={() => switchScreen("login")} role="tab" type="button">登录</button>
            <button aria-selected={screen === "register"} className={`min-h-10 rounded-[4px] text-sm font-medium ${screen === "register" ? "bg-white text-sky-700 shadow-sm" : "text-slate-600 hover:text-slate-900"}`} onClick={() => switchScreen("register")} role="tab" type="button">邀请码注册</button>
          </div>

          <div className="space-y-4">
            <label className="block" htmlFor="account">
              <span className="mb-1.5 block text-sm font-medium text-slate-700">手机号或管理员账号</span>
              <input autoComplete="username" className="min-h-11 w-full rounded-md border border-slate-300 px-3 text-base outline-none focus:border-sky-600 focus:ring-2 focus:ring-sky-100" id="account" inputMode={isAdminAccount(phone) ? "text" : "tel"} name="username" onChange={(event) => setPhone(event.target.value)} placeholder="手机号" required type="text" value={phone} />
            </label>

            <label className="block" htmlFor="password">
              <span className="mb-1.5 block text-sm font-medium text-slate-700">{screen === "register" ? "设置密码" : "密码"}</span>
              <span className="relative block">
                <input autoComplete={screen === "register" ? "new-password" : "current-password"} className="min-h-11 w-full rounded-md border border-slate-300 px-3 pr-12 text-base outline-none focus:border-sky-600 focus:ring-2 focus:ring-sky-100" id="password" minLength={screen === "register" ? 8 : undefined} name="password" onChange={(event) => setPassword(event.target.value)} placeholder={screen === "register" ? "至少 8 位" : "输入密码"} required type={showPassword ? "text" : "password"} value={password} />
                <button aria-label={showPassword ? "隐藏密码" : "显示密码"} className="icon-button absolute right-0.5 top-0.5 text-slate-500 hover:text-slate-900" onClick={() => setShowPassword((value) => !value)} title={showPassword ? "隐藏密码" : "显示密码"} type="button"><Icon name={showPassword ? "eye-off" : "eye"} size={18} /></button>
              </span>
              {screen === "register" && <span className="mt-1 block text-xs text-slate-500">至少 8 位字符。</span>}
            </label>

            {screen === "register" && (
              <>
                <label className="block" htmlFor="confirm-password">
                  <span className="mb-1.5 block text-sm font-medium text-slate-700">确认密码</span>
                  <input aria-invalid={passwordMismatch} autoComplete="new-password" className={`min-h-11 w-full rounded-md border px-3 text-base outline-none focus:ring-2 ${passwordMismatch ? "border-red-400 focus:ring-red-100" : "border-slate-300 focus:border-sky-600 focus:ring-sky-100"}`} id="confirm-password" name="confirm-password" onChange={(event) => setConfirmPassword(event.target.value)} required type={showPassword ? "text" : "password"} value={confirmPassword} />
                  {passwordMismatch && <span className="mt-1 block text-xs text-red-600">两次输入的密码不一致。</span>}
                </label>
                <label className="block" htmlFor="invite-code">
                  <span className="mb-1.5 block text-sm font-medium text-slate-700">邀请码</span>
                  <input autoComplete="off" className="min-h-11 w-full rounded-md border border-slate-300 px-3 text-base uppercase outline-none focus:border-sky-600 focus:ring-2 focus:ring-sky-100" id="invite-code" name="invite-code" onChange={(event) => setInviteCode(event.target.value)} placeholder="输入已发放的邀请码" required value={inviteCode} />
                </label>
              </>
            )}
          </div>

          <div aria-live="polite">
            {error && <div className="rounded-md border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700" role="alert">{error}</div>}
          </div>

          <button className="flex min-h-11 w-full items-center justify-center gap-2 rounded-md bg-sky-700 px-4 text-sm font-semibold text-white hover:bg-sky-800 disabled:cursor-not-allowed disabled:opacity-45" disabled={!canSubmit} type="submit">
            {loading && <span className="h-4 w-4 animate-spin rounded-full border-2 border-white border-t-transparent" />}
            {loading ? "正在处理" : screen === "register" ? "注册并登录" : "登录"}
          </button>
        </form>
      </div>
    </main>
  );
}
