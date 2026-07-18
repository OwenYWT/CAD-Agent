import { useMemo, useState } from "react";
import type { AuthUser } from "../../auth";
import { adaptEngineeringProject } from "../../adapters/projectAdapter";
import { useWebSocket } from "../../hooks/useWebSocket";
import { useSessionStore } from "../../stores/sessionStore";
import type { EngineeringDomain, EngineeringStage } from "../../types/engineering";
import AgentDrawer from "../agent/AgentDrawer";
import ExportDialog from "../export/ExportDialog";
import ParameterDrawer from "../parameters/ParameterDrawer";
import ProjectFlow from "../project/ProjectFlow";
import ProjectSidebar from "../project/ProjectSidebar";
import ProjectStart from "../project/ProjectStart";
import WorkspaceHeader from "../project/WorkspaceHeader";
import SettingsDrawer from "../SettingsDrawer";
import ValidationDialog from "../validation/ValidationDialog";
import MechanicalWorkspace from "../viewer/MechanicalWorkspace";
import { Icon } from "../ui/Icon";

interface EngineeringWorkspaceProps {
  user: AuthUser;
  onLogout: () => void;
  onUserUpdate: (user: AuthUser) => void;
}

export default function EngineeringWorkspace({ user, onLogout, onUserUpdate }: EngineeringWorkspaceProps) {
  const sessionId = useSessionStore((state) => state.sessionId);
  const panel = useSessionStore((state) => state.getActivePanel());
  const { connectionState, sendMessage, executeCode, restoreContext } = useWebSocket();
  const model = useMemo(() => adaptEngineeringProject(sessionId, panel), [panel, sessionId]);
  const hasProject = panel.messages.length > 0 || panel.result !== null || panel.isGenerating;
  const [view, setView] = useState<EngineeringDomain>("overview");
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  const [mobileSidebar, setMobileSidebar] = useState(false);
  const [startHistory, setStartHistory] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [agentOpen, setAgentOpen] = useState(false);
  const [agentPrompt, setAgentPrompt] = useState("");
  const [parametersOpen, setParametersOpen] = useState(false);
  const [checksOpen, setChecksOpen] = useState(false);
  const [exportOpen, setExportOpen] = useState(false);

  const startProject = (prompt: string) => {
    if (!sendMessage(prompt)) return false;
    useSessionStore.getState().beginGeneration();
    useSessionStore.getState().addMessage({ role: "user", content: prompt });
    setView("overview");
    return true;
  };

  const navigate = (next: string) => setView(next as EngineeringDomain);
  const openStage = (stage: EngineeringStage) => {
    if (!stage.available) return;
    if (stage.id === "manufacturing") { setChecksOpen(true); return; }
    if (stage.id === "release") { setExportOpen(true); return; }
    if (stage.domain) setView(stage.domain);
  };
  const askAgent = (prompt = "") => { setAgentPrompt(prompt); setAgentOpen(true); };
  const executeWithProgress = (code: string) => {
    if (!executeCode(code)) return false;
    useSessionStore.getState().beginGeneration();
    return true;
  };

  if (!hasProject) {
    return <>
      <ProjectStart connectionState={connectionState} onOpenHistory={() => setStartHistory(true)} onStart={startProject} />
      {startHistory ? (
        <div className="start-history-overlay">
          <button aria-label="关闭历史项目" className="start-history-overlay__backdrop" onClick={() => setStartHistory(false)} type="button" />
          <ProjectSidebar activeView="overview" collapsed={false} mobileOpen onCollapse={() => {}} onMobileClose={() => setStartHistory(false)} onNavigate={() => setStartHistory(false)} restoreContext={restoreContext} />
        </div>
      ) : null}
    </>;
  }

  return (
    <div className="flex h-[100dvh] min-w-[320px] flex-col overflow-hidden bg-white text-[var(--ink)]">
      <WorkspaceHeader connection={connectionState} onAgent={() => askAgent()} onBack={() => setView("overview")} onChecks={() => setChecksOpen(true)} onExport={() => setExportOpen(true)} onLogout={onLogout} onMenu={() => setMobileSidebar(true)} onSettings={() => setSettingsOpen(true)} onUserUpdate={onUserUpdate} project={model.project} user={user} />
      <div className="flex min-h-0 flex-1">
        <ProjectSidebar activeView={view} collapsed={sidebarCollapsed} mobileOpen={mobileSidebar} onCollapse={() => setSidebarCollapsed((value) => !value)} onMobileClose={() => setMobileSidebar(false)} onNavigate={navigate} restoreContext={restoreContext} />
        <div className="min-w-0 flex-1">
          {view === "overview" ? <ProjectFlow onOpenStage={openStage} project={model.project} stages={model.stages} task={model.task} /> : null}
          {view === "mechanical" ? <MechanicalWorkspace onAgent={() => askAgent()} onBack={() => setView("overview")} onProperties={() => setParametersOpen(true)} result={model.result} /> : null}
        </div>
      </div>
      <button aria-label="询问 Agent" className="fixed bottom-4 right-4 z-30 grid h-12 w-12 place-items-center rounded-full bg-[var(--ink)] text-white shadow-xl sm:hidden" onClick={() => askAgent()} type="button"><Icon name="message" size={18} /></button>

      <ParameterDrawer isGenerating={panel.isGenerating} key={`parameters:${model.result?.request_id || "empty"}:${parametersOpen}`} onClose={() => setParametersOpen(false)} onExecute={executeWithProgress} open={parametersOpen} parameters={model.parameters} result={model.result} />
      <AgentDrawer connection={connectionState} context={view} key={`${view}:${agentPrompt}:${agentOpen}`} onClose={() => { setAgentOpen(false); setAgentPrompt(""); }} onSend={sendMessage} open={agentOpen} suggestedPrompt={agentPrompt} />
      <ValidationDialog description={panel.messages.filter((message) => message.role === "user").at(-1)?.content || ""} key={`validation:${model.result?.request_id || "empty"}:${checksOpen}`} onAskAgent={(prompt) => { setChecksOpen(false); askAgent(prompt); }} onClose={() => setChecksOpen(false)} open={checksOpen} result={model.result} />
      <ExportDialog jobs={model.exports} onClose={() => setExportOpen(false)} open={exportOpen} />
      <SettingsDrawer onClose={() => setSettingsOpen(false)} open={settingsOpen} />
    </div>
  );
}
