import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import type { ArtifactUpdateEvent, DurableBOMProjection, FreeCADBOMDocument, GenerationResult } from "../../types";
import type { EngineeringDomain, EngineeringStage, Parameter, Project } from "../../types/engineering";
import { downloadEngineeringArtifact, getRevisionBOM } from "../../services/engineeringService";
import { Icon } from "../ui/Icon";

type InspectorTab = "document" | "parameters" | "versions" | "code" | "files" | "bom";

interface WorkspaceInspectorProps {
  cloudDocument?: ReactNode;
  view: EngineeringDomain;
  project: Project;
  stages: EngineeringStage[];
  parameters: Parameter[];
  result: GenerationResult | null;
  artifacts: ArtifactUpdateEvent[];
  versionHistory: ReactNode;
  onProperties: () => void;
  onExport?: () => void;
  onCollapse?: () => void;
  showHeader?: boolean;
  projectId?: string | null;
  revisionId?: string | null;
  bom?: DurableBOMProjection | null;
}

const TABS: Array<[InspectorTab, string]> = [
  ["document", "文档"],
  ["parameters", "属性"],
  ["versions", "版本"],
  ["code", "代码"],
  ["files", "文件"],
  ["bom", "BOM"],
];

function fileLabel(format: string) {
  return format.replace(/^\./, "").toUpperCase();
}

export default function WorkspaceInspector({
  cloudDocument,
  view,
  project,
  stages,
  parameters,
  result,
  artifacts,
  versionHistory,
  onProperties,
  onExport,
  onCollapse,
  showHeader = true,
  projectId = null,
  revisionId = null,
  bom = null,
}: WorkspaceInspectorProps) {
  const [tab, setTab] = useState<InspectorTab>(cloudDocument ? "document" : "parameters");
  const [downloadError, setDownloadError] = useState("");
  const [downloading, setDownloading] = useState<string | null>(null);
  const downloadFile = async (url: string, fallbackName: string) => {
    setDownloadError("");
    setDownloading(url);
    try {
      const filename = url.split("?")[0].split("/").pop() || fallbackName;
      await downloadEngineeringArtifact(url, filename);
    } catch (error) {
      setDownloadError(error instanceof Error ? error.message : "文件下载失败");
    } finally {
      setDownloading(null);
    }
  };
  const files = useMemo(() => Object.entries(result?.files || {}), [result?.files]);
  const [bomRead, setBomRead] = useState<{
    key: string;
    document: FreeCADBOMDocument | null;
    error: string | null;
  } | null>(null);
  const bomReadToken = useRef(0);
  const bomRequestKey = projectId && revisionId
    ? `${projectId}:${revisionId}:${bom?.evidence_id || "none"}`
    : null;

  useEffect(() => {
    if (
      bom?.status !== "succeeded"
      || !projectId
      || !revisionId
      || !bomRequestKey
      || (bom.revision_id && bom.revision_id !== revisionId)
    ) {
      return;
    }
    const token = ++bomReadToken.current;
    const controller = new AbortController();
    void getRevisionBOM(projectId, revisionId, controller.signal)
      .then((document) => {
        if (token !== bomReadToken.current || controller.signal.aborted) return;
        if (!document.rows.length) {
          setBomRead({ key: bomRequestKey, document: null, error: "bom_empty" });
          return;
        }
        setBomRead({ key: bomRequestKey, document, error: null });
      })
      .catch((error: unknown) => {
        if (token !== bomReadToken.current || controller.signal.aborted) return;
        setBomRead({
          key: bomRequestKey,
          document: null,
          error: error instanceof Error ? error.message : "BOM 加载失败",
        });
      });
    return () => controller.abort();
  }, [bom?.revision_id, bom?.status, bomRequestKey, projectId, revisionId]);

  const bomStale = Boolean(
    bom?.revision_id && revisionId && bom.revision_id !== revisionId,
  );
  const bomLoading = Boolean(bom?.status === "pending"
    || bom?.status === "running"
    || (
      bom?.status === "succeeded"
      && bomRequestKey
      && bomRead?.key !== bomRequestKey
    ));
  const bomDocument = bomRead?.key === bomRequestKey ? bomRead.document : null;
  const bomError = bomRead?.key === bomRequestKey ? bomRead.error : null;
  const bomState = bomStale
    ? "stale_revision"
    : bomLoading
      ? "loading"
      : bomError
        ? "failed"
        : bom?.status || "missing";

  if (view !== "mechanical") {
    return (
      <div className="ww-pane-content">
        {showHeader ? <div className="ww-pane-header"><div><p className="ww-pane-eyebrow">当前项目</p><h2>检查器</h2></div>{onCollapse ? <button aria-label="折叠检查器" className="workspace-icon-button" onClick={onCollapse} type="button"><Icon name="minus" size={15} /></button> : null}</div> : null}
        <div className="ww-pane-scroll">
          <div className="ww-project-summary"><span>项目</span><strong data-i18n-skip>{project.name}</strong><span>分支</span><strong data-i18n-skip>{project.branch}</strong><span>阶段</span><strong>{stages.filter((stage) => stage.status === "completed").length} / {stages.length} 已完成</strong><span>状态</span><strong>{project.status === "in_progress" ? "进行中" : project.status === "completed" ? "已完成" : "等待任务"}</strong></div>
        </div>
      </div>
    );
  }

  return (
    <div className="ww-pane-content ww-inspector-content">
      {showHeader ? <div className="ww-pane-header"><div><p className="ww-pane-eyebrow">机械设计</p><h2>检查器 {!cloudDocument && result?.version ? <span className="ww-inspector-version">v{result.version}</span> : null}</h2></div>{onCollapse ? <button aria-label="折叠检查器" className="workspace-icon-button" onClick={onCollapse} type="button"><Icon name="minus" size={15} /></button> : null}</div> : null}
      <div className="ww-inspector-tabs" role="tablist" aria-label="机械设计检查器">
        {TABS.filter(([id]) => id !== "document" || cloudDocument).map(([id, label]) => <button aria-selected={tab === id} className={tab === id ? "is-active" : ""} key={id} onClick={() => setTab(id)} role="tab" type="button">{label}</button>)}
      </div>
      <div className="ww-pane-scroll">
        {tab === "document" ? cloudDocument : null}
        {tab === "parameters" ? (
          <div className="ww-inspector-section">
            <div className="ww-inspector-section__header"><div><h3>模型属性</h3><p>显示当前结果解析出的真实参数。</p></div><button className="workspace-button" onClick={onProperties} type="button"><Icon name="sliders" size={14} />编辑</button></div>
            {parameters.length ? <div className="ww-parameter-list">{parameters.map((parameter) => <div key={parameter.id}><span><strong>{parameter.label}</strong><small>{parameter.group}</small></span><span>{parameter.value}{parameter.unit}</span></div>)}</div> : <p className="ww-inspector-empty">当前结果没有可编辑参数。生成参数化模型后会显示真实参数。</p>}
          </div>
        ) : null}

        {tab === "versions" ? <div className="ww-version-history-host">{versionHistory}</div> : null}

        {tab === "code" ? (
          <div className="ww-inspector-section">
            <div className="ww-inspector-section__header"><div><h3>生成代码</h3><p>{result?.code ? `当前版本 v${result.version ?? "-"}` : "暂无可显示代码"}</p></div></div>
            {result?.code ? <pre className="ww-code-preview"><code>{result.code}</code></pre> : <p className="ww-inspector-empty">生成成功后，这里会显示后端返回的当前模型代码。</p>}
          </div>
        ) : null}

        {tab === "files" ? (
          <div className="ww-inspector-section">
            <div className="ww-inspector-section__header"><div><h3>工程文件</h3><p>当前结果和实时任务返回的产物。</p></div></div>
            {files.length ? <div className="ww-file-list">{files.map(([format, url]) => <button disabled={downloading !== null} key={format} onClick={() => void downloadFile(url, `model.${format}`)} type="button"><Icon name="file" size={14} /><span><strong>{fileLabel(format)}{downloading === url ? " · 正在下载" : ""}</strong><small>{url}</small></span><Icon name="download" size={13} /></button>)}</div> : <p className="ww-inspector-empty">当前结果尚未返回可用工程文件。</p>}
            {downloadError ? <p className="ww-inspector-empty" role="alert">{downloadError}</p> : null}
            {artifacts.length ? <div className="ww-artifact-list"><h4>任务产物事件</h4>{artifacts.slice(-10).reverse().map((artifact, index) => <div key={`${artifact.path}:${index}`}><span>{artifact.artifact_type.toUpperCase()}</span><code>{artifact.path}</code></div>)}</div> : null}
          </div>
        ) : null}

        {tab === "bom" ? (
          <div className="ww-inspector-section">
            <div className="ww-inspector-section__header"><div><h3>物料清单</h3><p>来自当前版本持久化的 FreeCAD Assembly 原生 BOM。</p></div>{bomState === "succeeded" && bom?.csv_download_url ? <button className="workspace-button" onClick={() => void downloadEngineeringArtifact(bom.csv_download_url!, "bom.csv")} type="button"><Icon name="download" size={14} />CSV</button> : null}</div>
            {bomState === "loading" ? <p className="ww-inspector-empty">正在读取已验证的 BOM…</p> : null}
            {bomState === "not_applicable" ? <p className="ww-inspector-empty">当前版本不是装配体，无需生成 BOM。</p> : null}
            {bomState === "missing" ? <p className="ww-inspector-empty">当前历史版本没有 BOM 证据。</p> : null}
            {bomState === "unsupported" ? <p className="ww-inspector-empty">当前 FreeCAD 运行时不支持原生 Assembly BOM。</p> : null}
            {bomState === "cancelled" ? <p className="ww-inspector-empty">BOM 生成已取消。</p> : null}
            {bomState === "stale_revision" ? <p className="ww-inspector-empty">BOM 属于另一版本，请切回对应版本后查看。</p> : null}
            {bomState === "failed" ? <p className="ww-inspector-empty">BOM 生成或读取失败：{bom?.error?.code || bomError || "bom_generation_failed"}</p> : null}
            {bomState === "succeeded" && bomDocument ? <div className="overflow-auto rounded-lg border border-[var(--line)]"><table className="w-full min-w-[520px] border-collapse type-caption"><thead><tr>{bomDocument.columns.map((column) => <th className="border-b border-[var(--line)] px-3 py-2 text-left" key={column}>{column}</th>)}</tr></thead><tbody>{bomDocument.rows.map((row) => <tr key={`${row.index}:${row.name}`}><td className="border-b border-[var(--line)] px-3 py-2">{row.index}</td><td className="border-b border-[var(--line)] px-3 py-2">{row.name}</td><td className="border-b border-[var(--line)] px-3 py-2">{row.quantity}</td><td className="border-b border-[var(--line)] px-3 py-2">{row.file_name}</td>{Object.values(row.properties).map((value, index) => <td className="border-b border-[var(--line)] px-3 py-2" key={index}>{value}</td>)}</tr>)}</tbody></table></div> : null}
          </div>
        ) : null}
      </div>
      <div className="ww-inspector-export">
        <span>{files.length ? files.map(([format]) => fileLabel(format)).join(" · ") : "等待工程产物"}</span>
        <button className="workspace-button workspace-button--primary" disabled={!files.length} onClick={onExport} type="button"><Icon name="download" size={14} />导出</button>
      </div>
    </div>
  );
}
