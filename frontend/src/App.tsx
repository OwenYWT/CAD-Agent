import { useState, useCallback, useEffect } from "react";
import type { AuthSession, AuthUser } from "./auth";
import { getAuthUser, logoutAuthSession, onSessionCleared, fetchCurrentUser } from "./auth";
import { LoginPage } from "./components/LoginPage";
import { useSessionStore } from "./stores/sessionStore";
import { useWebSocket } from "./hooks/useWebSocket";
import ErrorBoundary from "./components/ErrorBoundary";
import ChatPanel from "./components/ChatPanel";
import Viewer3D from "./components/Viewer3D";
import Viewer2D from "./components/Viewer2D";
import ParameterPanel from "./components/ParameterPanel";
import MultiStepProgress from "./components/MultiStepProgress";
import DesignAnalysis from "./components/DesignAnalysis";
import AssemblyTree from "./components/AssemblyTree";
import PanelTabs from "./components/PanelTabs";
import HistorySidebar from "./components/HistorySidebar";
import SettingsDrawer from "./components/SettingsDrawer";
import DownloadPanel from "./components/DownloadPanel";
import { AccountPanel } from "./components/AccountPanel";
import type { Annotation3D } from "./types";

type RightTab = "params" | "analysis" | "download" | "code" | "assembly";

const TAB_ITEMS: { key: RightTab; label: string; icon: string }[] = [
  { key: "params", label: "参数", icon: "⚙" },
  { key: "analysis", label: "分析", icon: "🔍" },
  { key: "download", label: "下载", icon: "↓" },
  { key: "code", label: "代码", icon: "</>" },
];

function MainApp({ authUser, onLogout, onUserUpdate }: { authUser: AuthUser; onLogout: () => void; onUserUpdate: (user: AuthUser) => void }) {
  const panel = useSessionStore((s) => s.getActivePanel());
  const { sendMessage, executeCode, cancelGeneration, modifyPart } = useWebSocket();

  const result = panel.result;
  const multiStepProgress = panel.multiStepProgress;

  const stlUrl = result?.files?.stl || null;
  const svgUrl = result?.files?.svg || null;
  const is2D = !!(svgUrl && !stlUrl);

  const assemblyParts = result?.assembly_parts || null;
  const hasResult = !!result?.success;

  // Right toolbar tab state
  const [activeTab, setActiveTab] = useState<RightTab>("params");
  const [settingsOpen, setSettingsOpen] = useState(false);

  // Annotation state (shared between Viewer3D and DesignAnalysis)
  const [annotations, setAnnotations] = useState<Annotation3D[]>([]);
  const [showAnnotations, setShowAnnotations] = useState(true);
  const [selectedAnnotation, setSelectedAnnotation] = useState<string | null>(null);

  const handleAnnotationsReady = useCallback((anns: Annotation3D[]) => {
    setAnnotations(anns);
    setShowAnnotations(anns.length > 0);
    setSelectedAnnotation(null);
  }, []);

  const handleToggleAnnotations = useCallback(() => {
    setShowAnnotations((v) => !v);
  }, []);

  const handleCodeChange = (newCode: string) => {
    executeCode(newCode);
  };

  const handleSwitchTab = useCallback((tab: string) => {
    setActiveTab(tab as RightTab);
  }, []);

  // Build visible tabs
  const visibleTabs = [...TAB_ITEMS];
  if (assemblyParts && assemblyParts.length > 0) {
    visibleTabs.push({ key: "assembly", label: "装配体", icon: "🧩" });
  }

  return (
    <ErrorBoundary>
      <div className="h-screen flex flex-col bg-gray-50">
        {/* Unified Header */}
        <header className="h-12 bg-white border-b border-gray-200 flex items-center px-4 shrink-0 relative">
          {/* Left: Logo */}
          <h1 className="text-base font-semibold text-gray-800 shrink-0">CAD Agent Web</h1>

          {/* Center: Panel Tabs */}
          <div className="flex-1 flex justify-center px-4">
            <PanelTabs />
          </div>

          {/* Right: Actions */}
          <div className="flex items-center gap-2 shrink-0">
            <HistorySidebar />
            <div className="flex items-center gap-2 text-xs text-gray-500 border-l border-gray-200 pl-3">
              <span className="hidden md:inline max-w-[160px] truncate" title={authUser.phone}>{authUser.phone}</span>
              <AccountPanel user={authUser} onUserUpdate={onUserUpdate} onLogout={onLogout} />
            </div>
            <button
              onClick={() => setSettingsOpen(true)}
              className="p-1.5 text-gray-400 hover:text-gray-600 hover:bg-gray-100 rounded transition-colors"
              title="设置"
            >
              <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M10.325 4.317c.426-1.756 2.924-1.756 3.35 0a1.724 1.724 0 002.573 1.066c1.543-.94 3.31.826 2.37 2.37a1.724 1.724 0 001.066 2.573c1.756.426 1.756 2.924 0 3.35a1.724 1.724 0 00-1.066 2.573c.94 1.543-.826 3.31-2.37 2.37a1.724 1.724 0 00-2.573 1.066c-.426 1.756-2.924 1.756-3.35 0a1.724 1.724 0 00-2.573-1.066c-1.543.94-3.31-.826-2.37-2.37a1.724 1.724 0 00-1.066-2.573c-1.756-.426-1.756-2.924 0-3.35a1.724 1.724 0 001.066-2.573c-.94-1.543.826-3.31 2.37-2.37.996.608 2.296.07 2.572-1.065z" />
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15 12a3 3 0 11-6 0 3 3 0 016 0z" />
              </svg>
            </button>
          </div>
        </header>

        {/* Main content */}
        <div className="flex-1 flex flex-col md:flex-row overflow-hidden">
          {/* Chat Panel */}
          <div className="w-full md:w-1/3 md:min-w-[320px] md:max-w-[420px] h-1/2 md:h-full">
            <ChatPanel
              onSendMessage={sendMessage}
              onCancel={cancelGeneration}
              onSwitchTab={handleSwitchTab}
            />
          </div>

          {/* Right panel */}
          <div className="flex-1 flex flex-col overflow-hidden">
            {/* 3D / 2D Viewer */}
            <div className="flex-[3] min-h-0">
              <ErrorBoundary
                fallback={
                  <div className="w-full h-full bg-gray-100 flex items-center justify-center">
                    <p className="text-red-400">3D 渲染器出错，请刷新页面</p>
                  </div>
                }
              >
                {is2D ? (
                  <Viewer2D svgUrl={svgUrl} />
                ) : (
                  <Viewer3D
                    stlUrl={stlUrl}
                    annotations={annotations}
                    showAnnotations={showAnnotations}
                    selectedAnnotation={selectedAnnotation}
                    onSelectAnnotation={setSelectedAnnotation}
                    onToggleAnnotations={handleToggleAnnotations}
                  />
                )}
              </ErrorBoundary>
            </div>

            {/* Right Toolbar: Tab bar + content */}
            {hasResult && (
              <div className="flex-[2] min-h-0 flex flex-col border-t border-gray-200">
                {/* Tab bar */}
                <div className="flex items-center gap-0.5 px-3 py-1.5 bg-white border-b border-gray-100 shrink-0">
                  {visibleTabs.map((tab) => (
                    <button
                      key={tab.key}
                      onClick={() => setActiveTab(tab.key)}
                      className={`flex items-center gap-1 px-3 py-1.5 rounded-md text-xs font-medium transition-colors ${
                        activeTab === tab.key
                          ? "bg-indigo-50 text-indigo-700"
                          : "text-gray-500 hover:text-gray-700 hover:bg-gray-50"
                      }`}
                    >
                      <span className="text-sm">{tab.icon}</span>
                      <span>{tab.label}</span>
                    </button>
                  ))}
                </div>

                {/* Tab content */}
                <div className="flex-1 min-h-0 overflow-y-auto">
                  {activeTab === "params" && (
                    <ParameterPanel
                      params={result?.params || null}
                      code={result?.code || null}
                      onCodeChange={handleCodeChange}
                      validation={result?.validation || null}
                    />
                  )}

                  {activeTab === "analysis" && (
                    <>
                      <DesignAnalysis
                        requestId={result?.request_id || null}
                        code={result?.code || null}
                        description={
                          panel.messages
                            .filter((m) => m.role === "user")
                            .at(-1)?.content || ""
                        }
                        hasResult={hasResult}
                        is2D={is2D}
                        onAnnotationsReady={handleAnnotationsReady}
                        selectedAnnotation={selectedAnnotation}
                        onSelectAnnotation={setSelectedAnnotation}
                      />
                      {multiStepProgress && (
                        <MultiStepProgress steps={multiStepProgress} />
                      )}
                    </>
                  )}

                  {activeTab === "download" && (
                    <DownloadPanel
                      files={result?.files || null}
                      requestId={result?.request_id || null}
                      hasResult={hasResult}
                    />
                  )}

                  {activeTab === "code" && result?.code && (
                    <div className="p-3">
                      <pre className="text-xs bg-gray-900 text-green-300 rounded-lg p-4 overflow-x-auto max-h-[400px] overflow-y-auto leading-relaxed">
                        {result.code}
                      </pre>
                    </div>
                  )}

                  {activeTab === "assembly" && (
                    <AssemblyTree
                      parts={assemblyParts}
                      onModifyPart={modifyPart}
                      isGenerating={panel.isGenerating}
                    />
                  )}
                </div>
              </div>
            )}
          </div>
        </div>

        {/* Settings Drawer */}
        <SettingsDrawer open={settingsOpen} onClose={() => setSettingsOpen(false)} />
      </div>
    </ErrorBoundary>
  );
}

function App() {
  // Optimistically render from the cached user, but validate the token against the
  // backend on load (L5) so an expired/forged stored token doesn't grant a UI flash.
  const [authUser, setAuthUser] = useState<AuthUser | null>(() => getAuthUser());

  // Any session clear (logout, expiry, or a background 401 in authFetch) drops the
  // UI back to the login page instead of leaving a zombie authenticated view (H3).
  useEffect(() => {
    const unsubscribe = onSessionCleared(() => setAuthUser(null));
    return unsubscribe;
  }, []);

  // Revalidate the stored token once on mount; clear the session if it's no longer
  // valid. (Only runs when there is a cached user to check.)
  useEffect(() => {
    if (!getAuthUser()) return;
    let cancelled = false;
    fetchCurrentUser().then((user) => {
      if (cancelled) return;
      if (user) setAuthUser(user);
      else setAuthUser(null);
    });
    return () => {
      cancelled = true;
    };
  }, []);

  const handleLogin = (session: AuthSession) => {
    setAuthUser(session.user);
  };

  const handleLogout = async () => {
    await logoutAuthSession();
    setAuthUser(null);
  };

  if (!authUser) {
    return <LoginPage onLogin={handleLogin} />;
  }

  return <MainApp authUser={authUser} onLogout={handleLogout} onUserUpdate={setAuthUser} />;
}

export default App;
