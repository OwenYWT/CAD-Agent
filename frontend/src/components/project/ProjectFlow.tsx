import type { EngineeringStage, Project, Task } from "../../types/engineering";
import { Icon } from "../ui/Icon";

const STATUS = {
  not_started: { label: "未开始", className: "bg-stone-100 text-stone-600" },
  in_progress: { label: "进行中", className: "bg-sky-50 text-sky-700" },
  awaiting_confirmation: { label: "等待确认", className: "bg-amber-50 text-amber-800" },
  completed: { label: "已完成", className: "bg-emerald-50 text-emerald-700" },
  issue: { label: "存在问题", className: "bg-red-50 text-red-700" },
} as const;

interface ProjectFlowProps {
  project: Project;
  stages: EngineeringStage[];
  task: Task | null;
  onOpenStage: (stage: EngineeringStage) => void;
}

export default function ProjectFlow({ project, stages, task, onOpenStage }: ProjectFlowProps) {
  const current = stages.find((stage) => stage.available && stage.status === "in_progress") || stages.find((stage) => stage.available && stage.status === "awaiting_confirmation") || stages.find((stage) => stage.available && stage.status === "issue");
  return (
    <main className="h-full overflow-y-auto bg-white px-5 py-8 sm:px-8 lg:px-10">
      <div className="mx-auto w-full max-w-[960px]">
        <div className="text-[11px] text-[var(--faint)]">项目 / {project.name} / {project.branch}</div>
        <div className="mt-5 flex items-start gap-6">
          <div className="min-w-0 flex-1"><h1 className="text-2xl font-semibold tracking-[-0.035em] text-[var(--ink)]">{task ? "正在推进当前工程任务" : project.status === "not_started" ? "等待工程需求" : "项目工程结果"}</h1></div>
        </div>

        {task ? <div className="mt-6 flex items-center gap-3 rounded-lg border border-sky-200 bg-sky-50 px-4 py-3" role="status"><span className="h-4 w-4 animate-spin rounded-full border-2 border-sky-600 border-t-transparent" /><div><p className="text-sm font-medium text-sky-900">{task.title}</p><p className="mt-0.5 text-xs text-sky-700">{task.detail}</p></div></div> : null}

        <section className="mt-5 overflow-hidden rounded-xl border border-[var(--line)]" aria-label="工程流程">
          {stages.map((stage) => {
            const meta = STATUS[stage.status];
            return <div className="flex min-h-[72px] items-center gap-4 border-b border-[var(--line)] px-4 py-3 last:border-0 sm:px-5" key={stage.id}><span className={`grid h-7 w-7 shrink-0 place-items-center rounded-full text-xs font-semibold ${stage.status === "completed" ? "bg-emerald-50 text-emerald-700" : stage.status === "issue" ? "bg-red-50 text-red-700" : "bg-[var(--subtle)] text-[var(--muted)]"}`}>{stage.status === "completed" ? "✓" : stage.index}</span><div className="min-w-0 flex-1"><div className="flex flex-wrap items-center gap-2"><h2 className="text-sm font-medium text-[var(--ink)]">{stage.title}</h2><span className={`rounded-full px-2 py-0.5 text-[10px] font-medium ${meta.className}`}>{meta.label}</span>{!stage.available ? <span className="text-[10px] text-[var(--faint)]">后端能力未接入</span> : null}</div><p className="mt-1 text-xs leading-5 text-[var(--muted)]">{stage.summary}</p></div><button className={`workspace-button shrink-0 ${current?.id === stage.id ? "workspace-button--primary" : ""}`} disabled={!stage.available} onClick={() => onOpenStage(stage)} type="button">{stage.actionLabel}{stage.available ? <Icon className="hidden sm:block" name="chevron-right" size={14} /> : null}</button></div>;
          })}
        </section>

        {current ? <div className="mt-5 flex items-center gap-3 rounded-xl border border-sky-200 bg-sky-50/60 px-4 py-4"><span className="grid h-10 w-10 shrink-0 place-items-center rounded-lg bg-white text-sky-700"><Icon name="box" size={18} /></span><div className="min-w-0 flex-1"><p className="text-sm font-medium text-[var(--ink)]">建议下一步</p><p className="mt-1 text-xs text-[var(--muted)]">{current.summary}</p></div><button className="workspace-button workspace-button--primary" onClick={() => onOpenStage(current)} type="button">{current.actionLabel}<Icon name="chevron-right" size={14} /></button></div> : null}
      </div>
    </main>
  );
}
