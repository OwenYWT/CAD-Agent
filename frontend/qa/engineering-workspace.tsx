import { useState } from "react";
import { createRoot } from "react-dom/client";
import "../src/index.css";
import ProjectStart from "../src/components/project/ProjectStart";
import ProjectFlow from "../src/components/project/ProjectFlow";
import MechanicalWorkspace from "../src/components/viewer/MechanicalWorkspace";
import AgentDrawer from "../src/components/agent/AgentDrawer";
import ExportDialog from "../src/components/export/ExportDialog";
import ParameterDrawer from "../src/components/parameters/ParameterDrawer";
import ValidationDialog from "../src/components/validation/ValidationDialog";
import type { EngineeringDomain, EngineeringStage, ExportJob, Project } from "../src/types/engineering";
import { Icon } from "../src/components/ui/Icon";

const project: Project = {
  id: "qa-project",
  name: "参数化设备外壳",
  description: "QA fixture",
  branch: "概念/qa",
  status: "in_progress",
};

const stages: EngineeringStage[] = [
  { id: "requirements", index: 1, title: "需求与方案", summary: "工程需求已进入当前会话。", status: "completed", actionLabel: "查看需求", domain: "overview", available: true },
  { id: "mechanical", index: 2, title: "机械设计", summary: "正在生成参数化模型。", status: "in_progress", actionLabel: "查看机械设计", domain: "mechanical", available: true },
  { id: "electronics", index: 3, title: "电子设计", summary: "当前后端尚无 ECAD 数据接口。", status: "not_started", actionLabel: "查看电子设计", domain: "electronics", available: false },
  { id: "simulation", index: 4, title: "仿真验证", summary: "可运行现有设计分析。", status: "not_started", actionLabel: "打开仿真", domain: "simulation", available: false },
  { id: "firmware", index: 5, title: "固件", summary: "当前没有固件构建接口。", status: "not_started", actionLabel: "查看固件", domain: "firmware", available: false },
  { id: "manufacturing", index: 6, title: "制造检查", summary: "等待模型结果后运行。", status: "not_started", actionLabel: "工程检查", available: false },
  { id: "release", index: 7, title: "发布", summary: "生成产物后可导出。", status: "not_started", actionLabel: "导出", available: false },
];

const exportJobs: ExportJob[] = [
  { id: "step", format: "STEP", label: "STEP 三维模型", status: "failed", limitation: "等待生成结果" },
  { id: "stl", format: "STL", label: "STL 打印模型", status: "failed", limitation: "等待生成结果" },
  { id: "zip", format: "ZIP", label: "项目完整包", status: "failed", limitation: "当前后端未提供项目打包接口" },
];

export function QAWorkspace() {
  const [started, setStarted] = useState(false);
  const [view, setView] = useState<EngineeringDomain>("overview");
  const [agentOpen, setAgentOpen] = useState(false);
  const [checksOpen, setChecksOpen] = useState(false);
  const [exportOpen, setExportOpen] = useState(false);
  const [parametersOpen, setParametersOpen] = useState(false);
  if (!started) return <ProjectStart connectionState="connected" onOpenHistory={() => {}} onStart={() => { setStarted(true); return true; }} />;
  return <div className="flex h-[100dvh] min-w-[320px] flex-col overflow-hidden bg-white">
    <header className="workspace-header"><span className="workspace-brand">C</span><span className="text-sm font-semibold">{project.name}</span><span className="workspace-branch hidden md:inline-flex">{project.branch}</span><div className="ml-auto flex gap-1"><button className="workspace-button" onClick={() => setAgentOpen(true)} type="button"><Icon name="message" size={14} />询问 Agent</button><button className="workspace-button" onClick={() => setChecksOpen(true)} type="button">检查</button><button className="workspace-button workspace-button--primary" onClick={() => setExportOpen(true)} type="button">导出</button></div></header>
    <div className="flex min-h-0 flex-1"><aside className="workspace-sidebar hidden lg:flex"><nav className="space-y-1 p-2">{(["overview", "mechanical", "electronics", "simulation", "firmware"] as EngineeringDomain[]).map((id) => <button className={`workspace-nav-item ${view === id ? "workspace-nav-item--active" : ""}`} key={id} onClick={() => setView(id)} type="button"><Icon name={id === "mechanical" ? "box" : id === "firmware" ? "code" : "layers"} size={15} /><span>{({ overview: "项目流程", mechanical: "机械设计", electronics: "电子设计", simulation: "仿真验证", firmware: "固件" } as const)[id]}</span></button>)}</nav></aside><div className="min-w-0 flex-1">{view === "overview" ? <ProjectFlow onOpenStage={(stage) => { if (stage.domain) setView(stage.domain); }} project={project} stages={stages} task={{ id: "qa-task", title: "正在生成参数化模型", detail: "generating_code", status: "in_progress" }} /> : <MechanicalWorkspace currentStep={null} isGenerating={false} onAgent={() => setAgentOpen(true)} onBack={() => setView("overview")} onProperties={() => setParametersOpen(true)} result={null} />}</div></div>
    <ParameterDrawer isGenerating={false} onClose={() => setParametersOpen(false)} onExecute={() => true} open={parametersOpen} parameters={[]} result={null} />
    <AgentDrawer connection="connected" context={view} onClose={() => setAgentOpen(false)} onSend={() => true} open={agentOpen} />
    <ValidationDialog description="QA fixture" isGenerating={false} onAskAgent={() => { setChecksOpen(false); setAgentOpen(true); }} onClose={() => setChecksOpen(false)} onRerunCode={() => {}} onRestore={() => {}} onRetryPrompt={() => false} open={checksOpen} panelId="qa-panel" result={null} steps={[]} />
    <ExportDialog jobs={exportJobs} onClose={() => setExportOpen(false)} open={exportOpen} />
  </div>;
}

createRoot(document.getElementById("root")!).render(<QAWorkspace />);
