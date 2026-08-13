import { useCallback, useEffect, useState } from "react";
import { deleteHistoryProject, listHistoryProjects, restoreHistoryProject } from "../../services/engineeringService";
import { useSessionStore } from "../../stores/sessionStore";
import type { AssemblyPartInfo } from "../../types";
import type { HistoryProject } from "../../types/engineering";
import { Icon } from "../ui/Icon";
import PanelTabs from "../PanelTabs";

interface ProjectSidebarProps {
  collapsed: boolean;
  mobileOpen: boolean;
  onCollapse: () => void;
  onMobileClose: () => void;
  onNavigate: (view: string) => void;
  activeView: string;
  restoreContext: (panelId: string, code: string, assemblyParts?: AssemblyPartInfo[]) => void;
}

const NAV = [
  ["overview", "项目流程", "clock"],
  ["mechanical", "机械设计", "box"],
] as const;

export default function ProjectSidebar({ collapsed, mobileOpen, onCollapse, onMobileClose, onNavigate, activeView, restoreContext }: ProjectSidebarProps) {
  const [history, setHistory] = useState<HistoryProject[]>([]);
  const [status, setStatus] = useState<"idle" | "loading" | "error">("loading");
  const [error, setError] = useState("");
  const loadSession = useSessionStore((state) => state.loadSession);

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

  const restore = async (id: string) => {
    setStatus("loading");
    try {
      const project = await restoreHistoryProject(id);
      loadSession(project.sessionId, project.panels);
      project.panels.forEach((panel) => { if (panel.currentCode) restoreContext(panel.id, panel.currentCode); });
      onNavigate("overview");
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
      {mobileOpen ? <button aria-label="关闭项目导航" className="fixed inset-0 z-40 bg-slate-950/20 lg:hidden" onClick={onMobileClose} type="button" /> : null}
      <aside className={`workspace-sidebar ${collapsed ? "workspace-sidebar--collapsed" : ""} ${mobileOpen ? "workspace-sidebar--mobile-open" : ""}`}>
        <div className="flex h-14 items-center border-b border-[var(--line)] px-3 lg:hidden"><span className="text-sm font-semibold">项目导航</span><button className="workspace-icon-button ml-auto" onClick={onMobileClose} type="button"><Icon name="x" size={16} /></button></div>
        <nav className="space-y-1 p-2" aria-label="专业工作区">
          {NAV.map(([id, label, icon]) => <button className={`workspace-nav-item ${activeView === id ? "workspace-nav-item--active" : ""}`} key={id} onClick={() => { onNavigate(id); onMobileClose(); }} title={collapsed ? label : undefined} type="button"><Icon name={icon} size={16} /><span>{label}</span></button>)}
        </nav>
        {!collapsed ? <div className="border-y border-[var(--line)] px-2 py-2"><p className="mb-1 px-1 text-[10px] font-medium uppercase tracking-[0.08em] text-[var(--faint)]">工程线程</p><PanelTabs /></div> : null}
        <div className="mx-3 border-t border-[var(--line)]" />
        <div className="min-h-0 flex-1 overflow-y-auto p-2">
          {!collapsed ? <div className="mb-2 flex items-center justify-between px-2"><span className="text-[10px] font-medium uppercase tracking-[0.08em] text-[var(--faint)]">历史项目</span><button aria-label="刷新历史" className="workspace-icon-button !h-7 !w-7" onClick={() => void load()} type="button"><Icon name="rotate" size={13} /></button></div> : null}
          {status === "loading" && !collapsed ? <p className="px-2 py-3 text-xs text-[var(--muted)]">正在加载…</p> : null}
          {status === "error" && !collapsed ? <div className="px-2 py-3 text-xs text-red-700"><p>{error}</p><button className="mt-2 underline" onClick={() => void load()} type="button">重试</button></div> : null}
          {status === "idle" && history.length === 0 && !collapsed ? <p className="px-2 py-3 text-xs text-[var(--faint)]">暂无历史项目</p> : null}
          {!collapsed ? history.slice(0, 12).map((item) => <button className="group flex w-full items-center gap-2 rounded-md px-2 py-2 text-left hover:bg-[var(--subtle)]" key={item.id} onClick={() => void restore(item.id)} type="button"><Icon className="shrink-0 text-[var(--faint)]" name="file" size={14} /><span className="min-w-0 flex-1"><span className="block truncate text-xs text-[var(--ink)]">{item.name}</span><span className="block truncate text-[10px] text-[var(--faint)]">{new Date(item.updatedAt).toLocaleDateString("zh-CN")}</span></span><span aria-label="删除项目" className="opacity-0 group-hover:opacity-100" onClick={(event) => void remove(event, item.id)} role="button"><Icon name="trash" size={13} /></span></button>) : null}
        </div>
        <button className="workspace-nav-item m-2 border-t border-[var(--line)]" onClick={onCollapse} title={collapsed ? "展开侧栏" : "折叠侧栏"} type="button"><Icon name={collapsed ? "chevron-right" : "minus"} size={16} /><span>折叠侧栏</span></button>
      </aside>
    </>
  );
}
