import { useEffect, useState } from "react";
import type { AuthSession, AuthUser } from "./auth";
import { clearAuthSession, fetchAuthConfig, fetchCurrentUser, getAuthToken, getAuthUser, LOCAL_DEV_USER, logoutAuthSession, onSessionCleared } from "./auth";
import EngineeringWorkspace from "./components/workspace/EngineeringWorkspace";
import ErrorBoundary from "./components/ErrorBoundary";
import { LoginPage } from "./components/LoginPage";
import { BrandMark } from "./components/common/BrandMark";
import { LanguageSwitch } from "./i18n/LanguageSwitch";
import { useSessionStore } from "./stores/sessionStore";

function App() {
  const [authUser, setAuthUser] = useState<AuthUser | null>(null);
  const [authChecked, setAuthChecked] = useState(false);
  const [authDisabled, setAuthDisabled] = useState(false);

  useEffect(() => {
    let cancelled = false;
    const bootstrapAuth = async () => {
      const config = await fetchAuthConfig();
      if (cancelled) return;
      const disabled = Boolean(config?.auth_disabled);
      setAuthDisabled(disabled);
      if (disabled) {
        if (getAuthToken() || getAuthUser()) clearAuthSession();
        setAuthUser(LOCAL_DEV_USER);
      } else if (getAuthUser()) {
        setAuthUser(await fetchCurrentUser());
      }
      if (!cancelled) setAuthChecked(true);
    };
    void bootstrapAuth();
    return () => { cancelled = true; };
  }, []);

  useEffect(
    () => onSessionCleared(() => {
      if (!authDisabled) useSessionStore.getState().bindOwner(null);
      setAuthUser(authDisabled ? LOCAL_DEV_USER : null);
    }),
    [authDisabled],
  );

  const handleLogout = async () => {
    if (authDisabled) {
      setAuthUser(LOCAL_DEV_USER);
      return;
    }
    await logoutAuthSession();
    setAuthUser(null);
  };

  if (!authChecked && !authUser) {
    return (
      <main aria-busy="true" aria-live="polite" className="ww-auth-shell">
        <LanguageSwitch className="workspace-button ww-auth-language" />
        <section className="ww-auth-status-card">
          <BrandMark description="参数化建模工作台" nameAs="h1" size="login" />
          <span aria-hidden="true" className="ww-auth-spinner" />
          <p>正在检查本地登录配置...</p>
        </section>
      </main>
    );
  }

  if (!authUser) {
    return <LoginPage onLogin={(session: AuthSession) => setAuthUser(session.user)} />;
  }

  return <ErrorBoundary><EngineeringWorkspace onLogout={handleLogout} onUserUpdate={setAuthUser} user={authUser} /></ErrorBoundary>;
}

export default App;
