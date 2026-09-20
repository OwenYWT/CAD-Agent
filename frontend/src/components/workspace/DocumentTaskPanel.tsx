import { useEffect, useState } from "react";
import { confirmDurableTask, getDurableTaskSnapshot } from "../../services/clients/tasks";
import type { DurableTaskSnapshot } from "../../types";

const STATUS: Record<string,string> = { pending:"等待执行",planning:"正在规划",running:"正在执行",waiting_confirmation:"等待确认",
  succeeded:"执行完成，变更待审阅",failed:"执行失败",cancelled:"已取消",timed_out:"执行超时" };

export default function DocumentTaskPanel({ taskId, onReview }: { taskId: string; onReview: (id: string) => void }) {
  const [task,setTask] = useState<DurableTaskSnapshot | null>(null);
  const [error,setError] = useState("");
  const [pending,setPending] = useState(false);
  useEffect(() => {
    let stopped = false;
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      try {
        const snapshot = await getDurableTaskSnapshot(taskId);
        if (stopped) return;
        setTask(snapshot); setError("");
        if (["failed","cancelled","timed_out"].includes(snapshot.status)) return;
        if (snapshot.status === "succeeded" && (!snapshot.change_set || ["committed","rejected","rolled_back"].includes(snapshot.change_set.status))) return;
      } catch (e) { if (!stopped) setError(e instanceof Error ? e.message : "任务读取失败"); }
      if (!stopped) timer = setTimeout(() => void poll(),2000);
    };
    void poll();
    return () => { stopped = true; clearTimeout(timer); };
  },[taskId]);
  const confirm = async (accepted: boolean) => {
    setPending(true); setError("");
    try { await confirmDurableTask(taskId,accepted,accepted ? "确认共享文档修改计划" : "拒绝共享文档修改计划"); }
    catch (e) { setError(e instanceof Error ? e.message : "确认失败"); }
    finally { setPending(false); }
  };
  return <section className="ww-inspector-section" aria-label="文档任务">
    <h3>{task?.status === "succeeded" && task.change_set?.status === "committed" ? "版本已提交"
      : task?.status === "succeeded" && task.change_set?.status === "rejected" ? "变更已拒绝"
      : task?.status === "succeeded" && task.change_set?.status === "rolled_back" ? "版本已回退"
      : task ? STATUS[task.status] || task.status : "正在读取任务…"}</h3>
    {typeof task?.request_payload.objective === "string" ? <p className="type-caption break-words" data-i18n-skip>{task.request_payload.objective}</p> : null}
    {task?.confirmation ? <div className="mt-2 type-caption"><p>{task.confirmation.reason}</p>
      <p>{task.confirmation.affected_objects.map((o) => o.label).join("、")}</p>
      <div className="mt-2 flex gap-2"><button className="workspace-button" type="button" disabled={pending} onClick={() => void confirm(false)}>拒绝</button>
        <button className="workspace-button" type="button" disabled={pending} onClick={() => void confirm(true)}>确认并继续</button></div></div> : null}
    {task?.error_message ? <p role="alert" className="type-caption text-red-700">{task.error_message}</p> : null}
    {error ? <p role="alert" className="type-caption text-red-700">{error}</p> : null}
    {task?.change_set ? <button className="workspace-button mt-2" type="button" onClick={() => onReview(task.change_set!.id)}>审阅任务变更</button> : null}
  </section>;
}
