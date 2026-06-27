const TOKEN_KEY = "cad_agent_auth_token";
const USER_KEY = "cad_agent_auth_user";
const API_BASE = import.meta.env.VITE_API_BASE || "";

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
  localStorage.setItem(TOKEN_KEY, session.token);
  localStorage.setItem(USER_KEY, JSON.stringify(session.user));
}

export function clearAuthSession() {
  localStorage.removeItem(TOKEN_KEY);
  localStorage.removeItem(USER_KEY);
}

export function authHeaders(): HeadersInit {
  const token = getAuthToken();
  return token ? { Authorization: `Bearer ${token}` } : {};
}

export async function authFetch(input: RequestInfo | URL, init: RequestInit = {}) {
  const headers = new Headers(init.headers);
  const token = getAuthToken();
  if (token && !headers.has("Authorization")) {
    headers.set("Authorization", `Bearer ${token}`);
  }
  const response = await fetch(input, { ...init, headers });
  if (response.status === 401) {
    clearAuthSession();
  }
  return response;
}

export async function refreshAuthSession(): Promise<AuthSession | null> {
  const res = await authFetch(`${API_BASE}/api/auth/refresh`, { method: "POST" });
  if (!res.ok) return null;
  const session = (await res.json()) as AuthSession;
  saveAuthSession(session);
  return session;
}

export async function logoutAuthSession() {
  try {
    await authFetch(`${API_BASE}/api/auth/logout`, { method: "POST" });
  } finally {
    clearAuthSession();
  }
}

export async function deleteAuthAccount() {
  try {
    const res = await authFetch(`${API_BASE}/api/auth/account`, { method: "DELETE" });
    if (!res.ok) {
      const text = await res.text();
      throw new Error(text || "??????");
    }
  } finally {
    clearAuthSession();
  }
}
