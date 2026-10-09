import { lazy, Suspense, useEffect, useState } from "react";
import { acceptDocumentReviewLink } from "../../services/clients/documents";
import { useCloudDocument } from "../../hooks/useCloudDocument";
import CloudDocumentPanel from "./CloudDocumentPanel";
import DocumentTaskPanel from "./DocumentTaskPanel";
import ChangeSetDialog from "../changes/ChangeSetDialog";

const Viewer3D = lazy(() => import("../Viewer3D"));
const DocumentSceneViewer = lazy(() => import("../viewer/DocumentSceneViewer"));

function SharedView({ id }: { id: string }) {
  const cloud = useCloudDocument(id);
  const [taskId,setTaskId] = useState<string | null>(() => new URLSearchParams(window.location.search).get("task"));
  const [reviewId,setReviewId] = useState<string | null>(null);
  const selectTask = (task: string) => {
    setTaskId(task);
    const url = new URL(window.location.href); url.searchParams.set("task",task);
    window.history.replaceState(null,"",url);
  };
  return <main className="flex h-[100dvh] flex-col bg-white text-[var(--ink)]">
    <header className="workspace-header"><strong>{cloud.document?.can_edit ? "协作编辑" : "共享审阅"} · 已提交版本</strong><a className="workspace-button ml-auto" href="/">返回个人工作区</a></header>
    <div className="grid min-h-0 min-w-0 flex-1 grid-cols-1 overflow-auto md:grid-cols-[minmax(0,1fr)_360px] md:overflow-hidden">
      <div className="relative min-h-[360px] min-w-0 overflow-hidden"><Suspense fallback={<p role="status">正在加载模型…</p>}>
        {cloud.document?.fcstd ? <DocumentSceneViewer key={id} documentId={id} revisionId={cloud.document.head_revision_id} selectedId={cloud.selectedId} onSelect={cloud.select} />
          : <Viewer3D stlUrl={cloud.document?.mesh?.url || null} />}
      </Suspense></div>
      <aside className="min-h-0 min-w-0 overflow-y-auto border-l border-[var(--line)]">
        {taskId && cloud.document?.can_edit ? <DocumentTaskPanel key={taskId} taskId={taskId} onReview={setReviewId} /> : null}
        <CloudDocumentPanel connection={cloud} onSubmitted={cloud.document?.can_edit ? selectTask : undefined}
          onReview={cloud.document?.can_edit ? setReviewId : undefined} onTask={cloud.document?.can_edit ? selectTask : undefined} />
      </aside>
    </div>
    <ChangeSetDialog open={reviewId !== null} panelId={id} changeSetId={reviewId} onClose={() => setReviewId(null)} canCommit={cloud.document?.can_commit === true} />
  </main>;
}

export default function SharedDocumentWorkspace() {
  const params = new URLSearchParams(window.location.search);
  const id = params.get("document") || "";
  const workspace = params.get("workspace") || "";
  const [token] = useState(() => new URLSearchParams(window.location.hash.slice(1)).get("invite"));
  const [ready, setReady] = useState(!token);
  const [error, setError] = useState("");
  useEffect(() => {
    if (!token) return;
    let stopped = false;
    void acceptDocumentReviewLink(id, workspace, token).then(() => {
      if (!stopped) { window.history.replaceState(null, "", window.location.pathname + window.location.search); setReady(true); }
    }).catch((e: unknown) => { if (!stopped) setError(e instanceof Error ? e.message : "邀请接受失败"); });
    return () => { stopped = true; };
  }, [id, workspace, token]);
  if (!ready) return <main className="grid min-h-screen place-items-center"><div><p role={error ? "alert" : "status"}>{error || "正在接受项目审阅邀请…"}</p><a className="workspace-button mt-3" href="/">返回个人工作区</a></div></main>;
  return <SharedView id={id} />;
}
