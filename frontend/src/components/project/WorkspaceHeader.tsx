import type { ConnectionState } from "../../hooks/useWebSocket";
import type { Project } from "../../types/engineering";
import { LanguageSwitch } from "../../i18n/LanguageSwitch";
import { useI18n } from "../../i18n/I18nContext";
import { Icon } from "../ui/Icon";

interface HeaderProps {
  project: Project;
  connection: ConnectionState;
  onBack: () => void;
  onMenu: () => void;
  onAgent: () => void;
  onChecks: () => void;
  onChanges: () => void;
  onExport: () => void;
  onSettings: () => void;
  onPreview: () => void;
  previewOpen: boolean;
}

export default function WorkspaceHeader(props: HeaderProps) {
  const { translate } = useI18n();
  return (
    <header className="workspace-header">
      <button aria-label="打开项目导航" className="workspace-icon-button lg:hidden" onClick={props.onMenu} type="button"><Icon name="layers" size={17} /></button>
      <button aria-label={translate(`返回项目流程：${props.project.name}`)} className="workspace-header__title" onClick={props.onBack} title="返回项目流程" type="button"><span data-i18n-skip>{props.project.name}</span></button>
      <span className="workspace-branch hidden md:inline-flex"><Icon name="history" size={13} /><span data-i18n-skip>{translate(props.project.branch)}</span></span>
      <span className={`workspace-status hidden xl:inline-flex ${props.connection === "connected" ? "text-emerald-700" : "text-amber-700"}`}><span className={props.connection === "connected" ? "bg-emerald-500" : "bg-amber-500"} />{props.connection === "connected" ? "已自动保存" : "实时连接中"}</span>
      <div className="workspace-header__actions">
        <button aria-label={props.previewOpen ? "返回 Agent" : "预览"} className="workspace-button workspace-header__preview-action" onClick={props.onPreview} type="button"><Icon name={props.previewOpen ? "message" : "eye"} size={15} /><span>{props.previewOpen ? "返回 Agent" : "预览"}</span></button>
        <button className="workspace-button workspace-header__agent-action" onClick={props.onAgent} type="button"><Icon name="message" size={15} />询问 Agent</button>
        <button aria-label="查看变更" className="workspace-button workspace-header__changes-action" onClick={props.onChanges} type="button"><Icon name="history" size={15} /><span className="hidden md:inline">查看变更</span></button>
        <button aria-label="检查" className="workspace-button" onClick={props.onChecks} type="button"><Icon name="shield-check" size={15} /><span className="hidden sm:inline">检查</span></button>
        <button aria-label="导出" className="workspace-button workspace-button--primary workspace-header__export-action" onClick={props.onExport} type="button"><Icon name="download" size={15} /><span className="hidden sm:inline">导出</span></button>
        <button aria-label="设置" className="workspace-icon-button workspace-header__settings-action" onClick={props.onSettings} type="button"><Icon name="settings" size={16} /></button>
        <LanguageSwitch className="workspace-button ww-language-switch" />
      </div>
    </header>
  );
}
