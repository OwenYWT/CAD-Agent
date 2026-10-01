import { useState } from "react";
import { inspectDocumentFeature } from "../../services/clients/documents";
import type { CloudDocument, SemanticFeature, InspectionFieldName, InspectionPage } from "../../types/document";

const FIELDS: Record<InspectionFieldName, string> = {
  properties: "内核属性", constraints: "草图约束", geometry: "草图几何", topology: "拓扑测量", dependencies: "依赖对象",
};

export default function FeatureInspection({ document, feature }: { document: CloudDocument; feature: SemanticFeature }) {
  const [field, setField] = useState<InspectionFieldName>("properties");
  const [page, setPage] = useState<InspectionPage | null>(null);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState("");
  const read = async (requested: InspectionFieldName, offset = 0) => {
    setPending(true); setError(""); setField(requested);
    try {
      const result = await inspectDocumentFeature(document, feature, requested, offset);
      if (result.revision_id !== document.revision_id) throw new Error("内核细节版本不匹配");
      const object = result.objects.find((o) => o.name === feature.kernel_name);
      if (!object || object.error || !object[requested]) throw new Error("检查点没有返回所选对象");
      setPage(object[requested] || null);
    } catch (e) { setPage(null); setError(e instanceof Error ? e.message : "读取失败"); }
    finally { setPending(false); }
  };
  return <div className="ww-inspector-section">
    <h3>内核细节</h3>
    <p className="type-caption text-[var(--muted)]">读取当前已提交检查点，每次最多 16 条。</p>
    <div className="my-2 flex flex-wrap gap-2">{Object.entries(FIELDS).map(([name, label]) =>
      <button className="workspace-button" type="button" key={name} disabled={pending} aria-pressed={page !== null && name === field}
        onClick={() => void read(name as InspectionFieldName)}>{label}</button>)}</div>
    {pending ? <p role="status" className="type-caption">读取中…</p> : null}
    {error ? <p role="alert" className="type-caption text-red-700">{error}</p> : null}
    {page?.status === "unavailable" ? <p className="type-caption">此版本未记录该项；不能据此判断对象没有这些信息。</p> : null}
    {page?.status === "measured" ? <div data-i18n-skip>
      <p className="type-caption">共 {page.total} 条，本次显示 {page.returned} 条{(page.recorded || 0) < (page.total || 0) ? `；检查点记录了其中 ${page.recorded} 条` : ""}</p>
      <ol className="mt-2 space-y-2">{page.items?.map((item, i) => <li className="rounded border border-[var(--line)] p-2" key={i}>
        <pre className="whitespace-pre-wrap break-all type-caption">{typeof item === "string" ? item : JSON.stringify(item, null, 2)}</pre>
      </li>)}</ol>
      <div className="mt-2 flex gap-2">
        <button className="workspace-button" type="button" disabled={pending || !page.offset} onClick={() => void read(field, Math.max(0, (page.offset || 0) - 16))}>上一页</button>
        <button className="workspace-button" type="button" disabled={pending || (page.offset || 0) + (page.returned || 0) >= (page.recorded || 0) || !page.returned}
          onClick={() => void read(field, (page.offset || 0) + (page.returned || 0))}>下一页</button>
      </div>
    </div> : null}
  </div>;
}
