import type { AuthUser } from "../../auth";
import type { ConnectionState } from "../../hooks/useWebSocket";
import type { Project } from "../../types/engineering";
import { AccountPanel } from "../AccountPanel";
import { BrandMark } from "../common/BrandMark";
import { Icon } from "../ui/Icon";

interface HeaderProps {
  project: Project;
  user: AuthUser;
  connection: ConnectionState;
  onBack: () => void;
  onMenu: () => void;
  onAgent: () => void;
  onChecks: () => void;
  onExport: () => void;
  onSettings: () => void;
  onLogout: () => void;
  onUserUpdate: (user: AuthUser) => void;
}

export default function WorkspaceHeader(props: HeaderProps) {
  return (
    <header className="workspace-header">
      <button aria-label="打开项目导航" className="workspace-icon-button lg:hidden" onClick={props.onMenu} type="button"><Icon name="layers" size={17} /></button>
      <button aria-label={`WordsWave，返回项目流程：${props.project.name}`} className="flex min-w-0 items-center gap-2" onClick={props.onBack} title="返回项目流程" type="button"><BrandMark showName={false} /><span className="hidden max-w-52 truncate text-sm font-semibold sm:inline">{props.project.name}</span></button>
      <span className="workspace-branch hidden md:inline-flex"><Icon name="history" size={13} />{props.project.branch}</span>
      <span className={`workspace-status hidden xl:inline-flex ${props.connection === "connected" ? "text-emerald-700" : "text-amber-700"}`}><span className={props.connection === "connected" ? "bg-emerald-500" : "bg-amber-500"} />{props.connection === "connected" ? "已自动保存" : "实时连接中"}</span>
      <div className="ml-auto flex items-center gap-1">
        <button className="workspace-button hidden sm:inline-flex" onClick={props.onAgent} type="button"><Icon name="message" size={15} />询问 Agent</button>
        <button className="workspace-button" onClick={props.onChecks} type="button"><Icon name="shield-check" size={15} /><span className="hidden sm:inline">检查</span></button>
        <button className="workspace-button workspace-button--primary" onClick={props.onExport} type="button"><Icon name="download" size={15} /><span className="hidden sm:inline">导出</span></button>
        <button aria-label="设置" className="workspace-icon-button" onClick={props.onSettings} type="button"><Icon name="settings" size={16} /></button>
        <AccountPanel onLogout={props.onLogout} onUserUpdate={props.onUserUpdate} user={props.user} />
      </div>
    </header>
  );
}
