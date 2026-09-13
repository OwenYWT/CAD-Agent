import { useEffect } from "react";
import { useDraftGuardStore } from "../../stores/draftGuard";

export default function DraftLeaveDialog() {
  const { drafts, pending, stay, discardAndContinue } = useDraftGuardStore();
  const dirty = Object.keys(drafts).length > 0;
  useEffect(() => {
    if (!dirty) return;
    const warn = (event: BeforeUnloadEvent) => { event.preventDefault(); event.returnValue = ""; };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [dirty]);
  if (!pending) return null;
  return <div className="fixed inset-0 z-[100] grid place-items-center bg-black/25 p-6">
    <section role="dialog" aria-modal="true" aria-label="保留编辑草稿" className="max-w-md rounded-xl bg-white p-6 shadow-xl">
      <h2 className="type-section-heading">当前有未提交的编辑草稿</h2>
      <p className="my-4 type-body">{Object.values(drafts).map(d => d.label).join("、")}。切换后这些本地输入将被清除。</p>
      <div className="flex flex-wrap gap-2">
        <button autoFocus className="workspace-button workspace-button--primary" onClick={stay} type="button">继续编辑</button>
        <button className="workspace-button" onClick={discardAndContinue} type="button">放弃草稿并切换</button>
      </div>
    </section>
  </div>;
}
