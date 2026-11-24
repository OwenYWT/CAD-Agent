const TOKEN_KEY = "cad_agent_auth_token";
const USER_KEY = "cad_agent_auth_user";
const API_BASE = import.meta.env?.VITE_API_BASE || "";

export interface AuthUser {
  id: string;
  phone: string;
  registered_via: string;
  is_admin: boolean;
  created_at: string;
  last_login_at: string;
}

export interface AuthSession {
  token: string;
  user: AuthUser;
}

export interface AuthConfig {
  auth_required: boolean;
  api_key_required: boolean;
  auth_disabled: boolean;
}

export const LOCAL_DEV_USER: AuthUser = {
  id: "local-dev",
  phone: "本地开发模式（无需登录）",
  registered_via: "auth_disabled",
  is_admin: false,
  created_at: "",
  last_login_at: "",
};

export function getAuthToken(): string | null {
  return localStorage.getItem(TOKEN_KEY);
}

export function getAuthUser(): AuthUser | null {
  const raw = localStorage.getItem(USER_KEY);
  if (!raw) return null;
  try {
    return JSON.parse(raw) as AuthUser;
  } catch {
    return null;
  }
}

export function saveAuthSession(session: AuthSession) {
  localStorage.setItem(USER_KEY, JSON.stringify(session.user));
  localStorage.setItem(TOKEN_KEY, session.token);
}

// Listeners notified whenever the session is cleared (logout, expiry, 401).
// The app subscribes so a background 401 can immediately drop the UI back to the
// login page instead of leaving a "zombie" authenticated view that 401s forever.
type SessionListener = () => void;
const sessionListeners = new Set<SessionListener>();
const refreshListeners = new Set<SessionListener>();

export function onSessionRefreshed(listener: SessionListener): () => void {
  refreshListeners.add(listener);
  const subject = getAuthUser()?.id;
  const onStorage = (event: StorageEvent) => {
    if (event.storageArea === localStorage && event.key === TOKEN_KEY && event.oldValue && event.newValue
        && subject && getAuthUser()?.id === subject) listener();
  };
  window.addEventListener("storage", onStorage);
  return () => {
    refreshListeners.delete(listener);
    window.removeEventListener("storage", onStorage);
  };
}

export function onSessionCleared(listener: SessionListener): () => void {
  sessionListeners.add(listener);
  return () => sessionListeners.delete(listener);
}

export function clearAuthSession() {
  localStorage.removeItem(TOKEN_KEY);
  localStorage.removeItem(USER_KEY);
  sessionListeners.forEach((listener) => {
    try {
      listener();
    } catch {
      /* a misbehaving listener must not block session teardown */
    }
  });
}

export function authHeaders(): HeadersInit {
  const token = getAuthToken();
  return token ? { Authorization: `Bearer ${token}` } : {};
}

export async function authFetch(input: RequestInfo | URL, init: RequestInit = {}) {
  const headers = new Headers(init.headers ?? (input instanceof Request ? input.headers : undefined));
  const workspace = new URLSearchParams(window.location.search).get("workspace");
  if (workspace && /^[0-9a-f-]{36}$/i.test(workspace)) headers.set("X-Workspace-Tenant", workspace);
  const token = getAuthToken();
  if (token && !headers.has("Authorization")) {
    headers.set("Authorization", `Bearer ${token}`);
  }
  const response = await fetch(input, { ...init, headers });
  if (response.status === 401 && token && getAuthToken() === token
      && headers.get("Authorization") === `Bearer ${token}`) {
    // Token rejected/expired: tear down the session and notify the app so it can
    // route back to the login page.
    clearAuthSession();
  }
  return response;
}

// Validate the stored token against the backend (L5: don't trust localStorage on
// load). Returns the fresh user on success, null otherwise.
export async function fetchCurrentUser(): Promise<AuthUser | null> {
  const token = getAuthToken();
  if (!token) return null;
  try {
    const res = await authFetch(`${API_BASE}/api/auth/me`);
    if (token !== getAuthToken()) return getAuthToken() ? fetchCurrentUser() : null;
    if (!res.ok) return null;
    const data = (await res.json()) as { user: AuthUser };
    return data.user ?? null;
  } catch {
    return null;
  }
}

export async function fetchAuthConfig(): Promise<AuthConfig | null> {
  try {
    const res = await fetch(`${API_BASE}/api/auth/config`);
    if (!res.ok) return null;
    return (await res.json()) as AuthConfig;
  } catch {
    return null;
  }
}

export async function refreshAuthSession(): Promise<AuthSession | null> {
  const previous = getAuthToken();
  const res = await authFetch(`${API_BASE}/api/auth/refresh`, { method: "POST" });
  if (!res.ok) return null;
  const session = (await res.json()) as AuthSession;
  if (getAuthToken() !== previous) return null;
  saveAuthSession(session);
  refreshListeners.forEach(listener => {
    try { listener(); } catch { /* Other connections must still rotate. */ }
  });
  return session;
}

export async function logoutAuthSession() {
  const token = getAuthToken();
  try {
    await authFetch(`${API_BASE}/api/auth/logout`, { method: "POST" });
  } finally {
    if (getAuthToken() === token) clearAuthSession();
  }
}

export async function deleteAuthAccount() {
  const token = getAuthToken();
  let res: Response;
  try {
    res = await authFetch(`${API_BASE}/api/auth/account`, { method: "DELETE" });
  } catch {
    if (token !== getAuthToken()) return;
    const account = await fetchCurrentUser();
    if (token !== getAuthToken()) return;
    throw new Error(account ? "账号仍然有效，可以重试注销。" : "注销结果尚未确认，请恢复连接后重试。");
  }
  if (!res.ok) {
    const text = await res.text();
    throw new Error(text || "注销账号失败，请稍后重试");
  }
  if (getAuthToken() === token) clearAuthSession();
}
