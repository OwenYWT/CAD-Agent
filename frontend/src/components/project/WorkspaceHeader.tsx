import type { ConnectionState } from "../../hooks/useWebSocket";
import type { Project } from "../../types/engineering";
import type { DocumentViewMode } from "../../types/document";
import { LanguageSwitch } from "../../i18n/LanguageSwitch";
import { useI18n } from "../../i18n/I18nContext";
import { Icon } from "../ui/Icon";

interface HeaderProps {
  documentSynced?:boolean;
  project: Project;
  connection: ConnectionState;
  onBack: () => void;
  onMenu: () => void;
  onChecks: () => void;
  onProperties: () => void;
  onVersions: () => void;
  onTools: () => void;
  onExport: () => void;
  canExport?: boolean;
  viewMode?: DocumentViewMode;
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
      <span className={`workspace-status hidden xl:inline-flex ${props.connection === "connected" ? "text-emerald-700" : "text-amber-700"}`}><span className={props.connection === "connected" ? "bg-emerald-500" : "bg-amber-500"} />{props.documentSynced===false ? "文档同步中" : props.connection !== "connected" ? "实时连接中" : props.viewMode === "candidate" ? "候选未提交" : props.viewMode === "history" ? "历史只读" : "文档已同步"}</span>
      <div className="workspace-header__actions">
        <button aria-label={props.previewOpen ? "返回 Agent" : "预览"} className="workspace-button workspace-header__preview-action" onClick={props.onPreview} type="button"><Icon name={props.previewOpen ? "message" : "eye"} size={15} /><span>{props.previewOpen ? "返回 Agent" : "预览"}</span></button>
        <button aria-label="属性" className="workspace-button" onClick={props.onProperties} type="button"><Icon name="sliders" size={15} /><span className="hidden sm:inline">属性</span></button>
        <button aria-label="检查" className="workspace-button" onClick={props.onChecks} type="button"><Icon name="shield-check" size={15} /><span className="hidden sm:inline">检查</span></button>
        <button aria-label="版本" className="workspace-button" onClick={props.onVersions} type="button"><Icon name="history" size={15} /><span className="hidden sm:inline">版本</span></button>
        <button aria-label="导出" disabled={props.canExport === false} title={props.canExport === false ? "当前版本尚无可导出产物，或账号没有导出权限" : undefined} className="workspace-button workspace-button--primary workspace-header__export-action" onClick={props.onExport} type="button"><Icon name="download" size={15} /><span className="hidden sm:inline">导出</span></button>
        <button aria-label="更多工具" className="workspace-icon-button" onClick={props.onTools} title="更多工具" type="button"><Icon name="layers" size={16} /></button>
        <button aria-label="设置" className="workspace-icon-button workspace-header__settings-action" onClick={props.onSettings} type="button"><Icon name="settings" size={16} /></button>
        <LanguageSwitch className="workspace-button ww-language-switch" />
      </div>
    </header>
  );
}
