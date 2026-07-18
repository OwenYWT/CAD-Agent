import { useEffect, useState } from "react";
import type { AuthSession, AuthUser } from "./auth";
import { fetchCurrentUser, getAuthUser, logoutAuthSession, onSessionCleared } from "./auth";
import EngineeringWorkspace from "./components/workspace/EngineeringWorkspace";
import ErrorBoundary from "./components/ErrorBoundary";
import { LoginPage } from "./components/LoginPage";

function App() {
  const [authUser, setAuthUser] = useState<AuthUser | null>(() => getAuthUser());

  useEffect(() => onSessionCleared(() => setAuthUser(null)), []);
  useEffect(() => {
    if (!getAuthUser()) return;
    let cancelled = false;
    void fetchCurrentUser().then((user) => { if (!cancelled) setAuthUser(user); });
    return () => { cancelled = true; };
  }, []);

  const logout = async () => { await logoutAuthSession(); setAuthUser(null); };

  if (!authUser) return <LoginPage onLogin={(session: AuthSession) => setAuthUser(session.user)} />;
  return <ErrorBoundary><EngineeringWorkspace onLogout={logout} onUserUpdate={setAuthUser} user={authUser} /></ErrorBoundary>;
}

export default App;
