import type { EngineeringStage, Project, Task } from "../../types/engineering";
import { Icon } from "../ui/Icon";

const STATUS = {
  not_started: { label: "未开始", className: "bg-[var(--subtle)] text-[var(--muted)]" },
  in_progress: { label: "进行中", className: "bg-[var(--agent-soft)] text-[var(--agent)]" },
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
        <div className="type-caption text-[var(--faint)]">项目 / {project.name} / {project.branch}</div>
        <div className="mt-5 flex items-start gap-6">
          <div className="min-w-0 flex-1"><h1 className="type-display-heading  tracking-[-0.035em] text-[var(--ink)]">{task ? "正在推进当前工程任务" : project.status === "not_started" ? "等待工程需求" : "项目工程结果"}</h1></div>
        </div>

        {task ? <div className="mt-6 flex items-center gap-3 rounded-lg border border-[var(--agent-border)] bg-[var(--agent-soft)] px-4 py-3" role="status"><span className="h-4 w-4 animate-spin rounded-full border-2 border-[var(--agent)] border-t-transparent" /><div><p className="type-body  text-[var(--ink)]">{task.title}</p><p className="mt-0.5 type-body text-[var(--agent)]">{task.detail}</p></div></div> : null}

        <section className="mt-5 overflow-hidden rounded-xl border border-[var(--line)]" aria-label="工程流程">
          {stages.map((stage) => {
            const meta = STATUS[stage.status];
            const unavailableLabel = stage.limitation ? "暂未接入" : stage.status === "in_progress" ? "完成后可查看" : "等待前置阶段";
            return <div className="flex min-h-[72px] items-center gap-4 border-b border-[var(--line)] px-4 py-3 last:border-0 sm:px-5" key={stage.id}><span className={`grid h-7 w-7 shrink-0 place-items-center rounded-full type-body  ${stage.status === "completed" ? "bg-emerald-50 text-emerald-700" : stage.status === "issue" ? "bg-red-50 text-red-700" : "bg-[var(--subtle)] text-[var(--muted)]"}`}>{stage.status === "completed" ? "✓" : stage.index}</span><div className="min-w-0 flex-1"><div className="flex flex-wrap items-center gap-2"><h2 className="type-section-heading  text-[var(--ink)]">{stage.title}</h2><span className={`rounded-full px-2 py-0.5 type-caption  ${meta.className}`}>{meta.label}</span>{!stage.available ? <span className="type-caption text-[var(--faint)]">{unavailableLabel}</span> : null}</div><p className="mt-1 type-body  text-[var(--muted)]">{stage.summary}</p></div><button className={`workspace-button shrink-0 ${current?.id === stage.id ? "workspace-button--primary" : ""}`} disabled={!stage.available} onClick={() => onOpenStage(stage)} type="button">{stage.actionLabel}{stage.available ? <Icon className="hidden sm:block" name="chevron-right" size={14} /> : null}</button></div>;
          })}
        </section>

        {current ? <div className="mt-5 flex items-center gap-3 rounded-xl border border-[var(--agent-border)] bg-[var(--agent-soft)] px-4 py-4"><span className="grid h-10 w-10 shrink-0 place-items-center rounded-lg bg-white text-[var(--agent)]"><Icon name="box" size={18} /></span><div className="min-w-0 flex-1"><p className="type-body  text-[var(--ink)]">建议下一步</p><p className="mt-1 type-body text-[var(--muted)]">{current.summary}</p></div><button className="workspace-button workspace-button--primary" onClick={() => onOpenStage(current)} type="button">{current.actionLabel}<Icon name="chevron-right" size={14} /></button></div> : null}
      </div>
    </main>
  );
}
