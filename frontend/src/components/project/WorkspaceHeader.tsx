import { useEffect, useRef, useState } from "react";
import type { ConnectionState } from "../../hooks/useWebSocket";
import type { Project } from "../../types/engineering";
import type { DocumentViewMode } from "../../types/document";
import { LanguageSwitch } from "../../i18n/LanguageSwitch";
import { useI18n } from "../../i18n/I18nContext";
import { Icon } from "../ui/Icon";
import { workspaceStatus } from '../../adapters/workspaceStatus';
import type { DocumentSyncStatus } from '../../adapters/workspaceStatus';
interface HeaderProps {
  documentSynced?: boolean;
  documentSyncStatus?: DocumentSyncStatus;
  documentSyncError?: string | null;
  activeTool?: "properties" | "checks" | "versions" | "export" | "tools" | null;
  project: Project; connection: ConnectionState;
  onBack: () => void; onMenu: () => void; onChecks: () => void; onProperties: () => void;
  onVersions: () => void; onTools: () => void; onExport: () => void; onSettings: () => void;
  canExport?: boolean; viewMode?: DocumentViewMode; onPreview: () => void; previewOpen: boolean;
}
export default function WorkspaceHeader(props: HeaderProps) {
  const { translate } = useI18n();
  const [menuOpen, setMenuOpen] = useState(false);
  const menu = useRef<HTMLDivElement>(null), menuButton = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    if (!menuOpen) return;
    menu.current?.querySelector<HTMLButtonElement>("button")?.focus();
    const close = (event: PointerEvent) => { if (!menu.current?.contains(event.target as Node) && !menuButton.current?.contains(event.target as Node)) setMenuOpen(false); };
    const key = (event: KeyboardEvent) => { if (event.key === "Escape") { setMenuOpen(false); menuButton.current?.focus(); } };
    document.addEventListener("pointerdown", close); document.addEventListener("keydown", key);
    return () => { document.removeEventListener("pointerdown", close); document.removeEventListener("keydown", key); };
  }, [menuOpen]);
  const status = workspaceStatus(props.connection, props.documentSyncStatus || (props.documentSynced === false ? 'syncing' : 'synced'), props.viewMode);
  const actions = [
    { key: "properties", label: "属性", icon: "sliders", run: props.onProperties },
    { key: "checks", label: "检查", icon: "shield-check", run: props.onChecks },
    { key: "versions", label: "版本", icon: "history", run: props.onVersions },
  ] as const;
  const action = (callback: () => void) => { setMenuOpen(false); callback(); };
  return <header className="workspace-header" data-menu-open={menuOpen}>
    <button aria-label="打开项目导航" className="workspace-icon-button xl:hidden" onClick={props.onMenu} type="button"><Icon name="layers" size={17} /></button>
    <button aria-label={translate(`返回项目流程：${props.project.name}`)} className="workspace-header__title" onClick={props.onBack} title={props.project.name} type="button"><span data-i18n-skip>{props.project.name}</span></button>
    <span className="workspace-branch hidden md:inline-flex"><Icon name="history" size={13} /><span>{translate(props.project.branch)}</span></span>
    <span className="workspace-status hidden xl:inline-flex" data-status={status.tone} title={props.documentSyncError || undefined} role="status"><span />{status.label}</span>
    <div className="workspace-header__actions">
      <button aria-label={props.previewOpen ? "返回 Agent" : "预览"} className="workspace-button workspace-header__preview-action" onClick={props.onPreview} type="button"><Icon name={props.previewOpen ? "message" : "eye"} size={15} /><span>{props.previewOpen ? "返回 Agent" : "预览"}</span></button>
      {actions.map(item => <button key={item.key} aria-label={item.label} aria-pressed={props.activeTool === item.key} className="workspace-button workspace-header__secondary" onClick={item.run} type="button"><Icon name={item.icon} size={15} /><span className="hidden sm:inline">{item.label}</span></button>)}
      <button aria-label="导出" disabled={props.canExport === false} title={props.canExport === false ? "当前版本尚无可导出产物，或账号没有导出权限" : undefined} className="workspace-button workspace-button--primary workspace-header__export-action workspace-header__secondary" onClick={props.onExport} type="button"><Icon name="download" size={15} /><span className="hidden sm:inline">导出</span></button>
      <button ref={menuButton} aria-label="更多工具" aria-expanded={menuOpen} aria-controls="workspace-actions" className="workspace-icon-button" onClick={() => setMenuOpen(value => !value)} title="更多工具" type="button"><Icon name="layers" size={16} /></button>
      <button aria-label="设置" className="workspace-icon-button workspace-header__settings-action workspace-header__secondary" onClick={props.onSettings} type="button"><Icon name="settings" size={16} /></button>
      <LanguageSwitch className="workspace-button ww-language-switch workspace-header__secondary" />
    </div>
    {menuOpen ? <div id="workspace-actions" ref={menu} className="ww-header-menu" aria-label="工作区操作">
      {actions.map(item => <button className="workspace-button" aria-pressed={props.activeTool === item.key} key={item.key} type="button" onClick={() => action(item.run)}>{item.label}</button>)}
      <button className="workspace-button" disabled={props.canExport === false} type="button" onClick={() => action(props.onExport)}>导出</button>
      <button className="workspace-button" aria-pressed={props.activeTool === "tools"} type="button" onClick={() => action(props.onTools)}>更多工具</button>
      <button className="workspace-button" type="button" onClick={() => action(props.onSettings)}>设置</button>
      <LanguageSwitch className="workspace-button ww-language-switch" />
    </div> : null}
  </header>;
}
