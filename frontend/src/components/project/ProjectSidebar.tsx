import { useCallback, useEffect, useState } from "react";
import type { AuthUser } from "../../auth";
import { deleteHistoryProject } from "../../services/clients/documents";
import { listHistoryProjects, restoreHistoryProject } from "../../services/clients/history";
import { useSessionStore } from "../../stores/sessionStore";
import type { AssemblyPartInfo } from "../../types";
import type { HistoryProject } from "../../types/engineering";
import { Icon } from "../ui/Icon";
import { AccountPanel } from "../AccountPanel";
import { BrandMark } from "../common/BrandMark";
import PanelTabs from "../PanelTabs";
import { useI18n } from "../../i18n/I18nContext";
import { guardDraft } from "../../stores/draftGuard";

interface ProjectSidebarProps {
  collapsed: boolean;
  mobileOpen: boolean;
  onCollapse: () => void;
  onMobileClose: () => void;
  onNavigate: (view: string) => void;
  activeView: string;
  restoreContext: (panelId: string, code: string, assemblyParts?: AssemblyPartInfo[]) => void;
  user?: AuthUser;
  onLogout?: () => void;
  onUserUpdate?: (user: AuthUser) => void;
  onNewProject?: () => void;
  onSettings?: () => void;
}

const NAV = [
  ["mechanical", "机械设计", "box"],
  ["overview", "项目流程", "clock"],
] as const;

export default function ProjectSidebar({ collapsed, mobileOpen, onCollapse, onMobileClose, onNavigate, activeView, restoreContext, user, onLogout, onUserUpdate, onNewProject, onSettings }: ProjectSidebarProps) {
  const { locale, translate } = useI18n();
  const [history, setHistory] = useState<HistoryProject[]>([]);
  const [status, setStatus] = useState<"idle" | "loading" | "error">("loading");
  const [error, setError] = useState("");
  const loadSession = useSessionStore((state) => state.loadSession);
  const applyDurableSnapshot = useSessionStore((state) => state.applyDurableSnapshot);

  const load = useCallback(async () => {
    setStatus("loading");
    try {
      setHistory(await listHistoryProjects());
      setStatus("idle");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "历史项目加载失败");
      setStatus("error");
    }
  }, []);

  useEffect(() => {
    let cancelled = false;
    void listHistoryProjects().then((items) => {
      if (!cancelled) { setHistory(items); setStatus("idle"); }
    }).catch((reason: unknown) => {
      if (!cancelled) { setError(reason instanceof Error ? reason.message : "历史项目加载失败"); setStatus("error"); }
    });
    return () => { cancelled = true; };
  }, []);

  const restore = (id: string) => guardDraft(() => { void restoreSelectedProject(id); });
  const restoreSelectedProject = async (id: string) => {
    setStatus("loading");
    try {
      const project = await restoreHistoryProject(id);
      loadSession(project.sessionId, project.panels);
      project.panels.forEach((panel) => {
        if (panel.taskSnapshot) applyDurableSnapshot(panel.taskSnapshot, panel.id);
        if (panel.currentCode) restoreContext(panel.id, panel.currentCode);
      });
      onNavigate("mechanical");
      onMobileClose();
      setStatus("idle");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "项目恢复失败");
      setStatus("error");
    }
  };

  const remove = async (event: React.MouseEvent, id: string) => {
    event.stopPropagation();
    if (!window.confirm("确认删除这个历史项目？")) return;
    try {
      await deleteHistoryProject(id);
      setHistory((items) => items.filter((item) => item.id !== id));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "项目删除失败");
      setStatus("error");
    }
  };

  return (
    <>
      {mobileOpen ? <button aria-label="关闭项目导航" className="fixed inset-0 z-40 bg-black/25 lg:hidden" onClick={onMobileClose} type="button" /> : null}
      <aside className={`workspace-sidebar ${collapsed ? "workspace-sidebar--collapsed" : ""} ${mobileOpen ? "workspace-sidebar--mobile-open" : ""}`}>
        <div className="ww-sidebar-brand">
          <BrandMark nameClassName="type-section-heading tracking-[-0.02em]" showName={!collapsed} />
          <button aria-label={collapsed ? "展开侧栏" : "折叠侧栏"} className="workspace-icon-button ww-sidebar-collapse-control ml-auto" onClick={onCollapse} type="button"><Icon name={collapsed ? "chevron-right" : "minus"} size={15} /></button>
          <button aria-label="关闭项目导航" className="workspace-icon-button ww-sidebar-mobile-close ml-auto" onClick={onMobileClose} type="button"><Icon name="x" size={16} /></button>
        </div>
        <div className="ww-sidebar-scroll">
          {onNewProject ? <button className="ww-new-creation" onClick={() => { onNewProject(); onMobileClose(); }} title={collapsed ? "新建设计" : undefined} type="button"><Icon name="plus" size={15} /><span>新建设计</span></button> : null}
          {!collapsed ? <p className="ww-sidebar-heading">工作区</p> : null}
        <nav className="space-y-1" aria-label="专业工作区">
          {NAV.map(([id, label, icon]) => <button className={`workspace-nav-item ${activeView === id ? "workspace-nav-item--active" : ""}`} key={id} onClick={() => { onNavigate(id); onMobileClose(); }} title={collapsed ? label : undefined} type="button"><Icon name={icon} size={16} /><span>{label}</span></button>)}
        </nav>
        {!collapsed ? <div className="ww-sidebar-section"><p className="ww-sidebar-heading">工程线程</p><PanelTabs /></div> : null}
        <div className="min-h-0 flex-1">
          {!collapsed ? <div className="mb-1 flex items-center justify-between px-2"><span className="ww-sidebar-heading !m-0 !px-0">历史项目</span><button aria-label="刷新历史" className="workspace-icon-button !h-7 !w-7" onClick={() => void load()} type="button"><Icon name="rotate" size={13} /></button></div> : null}
          {status === "loading" && !collapsed ? <p className="px-2 py-3 type-body text-[var(--muted)]">正在加载…</p> : null}
          {status === "error" && !collapsed ? <div className="px-2 py-3 type-body text-red-700"><p data-i18n-skip>{error}</p><button className="mt-2 underline" onClick={() => void load()} type="button">重试</button></div> : null}
          {status === "idle" && history.length === 0 && !collapsed ? <p className="px-2 py-3 type-body text-[var(--faint)]">暂无历史项目</p> : null}
          {!collapsed ? history.slice(0, 12).map((item) => <div className="group flex w-full items-center rounded-md hover:bg-[var(--subtle)]" key={item.id}><button className="flex min-w-0 flex-1 items-center gap-2 px-2 py-2 text-left" onClick={() => void restore(item.id)} type="button"><Icon className="shrink-0 text-[var(--faint)]" name="file" size={14} /><span className="min-w-0 flex-1"><span className="block truncate type-body text-[var(--ink)]" data-i18n-skip>{item.name}</span><span className="block truncate type-caption text-[var(--faint)]">{new Date(item.updatedAt).toLocaleDateString(locale === "zh" ? "zh-CN" : "en-US")}</span></span></button><button aria-label={translate(`删除项目：${item.name}`)} className="workspace-icon-button mr-1 !h-7 !w-7 opacity-0 group-hover:opacity-100 focus:opacity-100" onClick={(event) => void remove(event, item.id)} type="button"><Icon name="trash" size={13} /></button></div>) : null}
        </div>
        {onSettings ? <button className="workspace-nav-item mt-2" onClick={() => { onSettings(); onMobileClose(); }} title={collapsed ? "设置" : undefined} type="button"><Icon name="settings" size={16} /><span>设置</span></button> : null}
        </div>
        {user && onLogout && onUserUpdate ? <div className="ww-sidebar-footer"><div className="ww-sidebar-user-copy">{user.registered_via === "auth_disabled" ? <span>{translate(user.phone)}</span> : <span data-i18n-skip>{user.phone}</span>}<small>{user.is_admin ? "管理员" : "工作区成员"}</small></div><AccountPanel onLogout={onLogout} onUserUpdate={onUserUpdate} user={user} /></div> : null}
      </aside>
    </>
  );
}
