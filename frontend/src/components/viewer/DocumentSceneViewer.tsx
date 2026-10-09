import CameraFit from "./CameraFit";
import CameraLink, {type CameraPose} from './CameraLink';
import ViewControls, { type StandardView } from "./ViewControls";
import { Grid, OrbitControls } from "@react-three/drei";
import { Canvas } from "@react-three/fiber";
import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { Box3, BufferGeometry, Color, InstancedMesh, Matrix4, Vector3, Plane } from "three";
import { STLLoader } from "three/examples/jsm/loaders/STLLoader.js";
import { authFetch } from "../../auth";
import { canvasEvents } from "./canvasEvents";
import { readDocumentScene, SceneJobError } from "../../services/sceneService";
import type { CloudDocument, SelectionContext } from "../../types/document";
import type { DocumentScene, SceneInstance, SceneLOD, SceneMesh } from "../../types/scene";

const API = import.meta.env.VITE_API_BASE || "";
const CACHE_BYTES = 128 * 1024 * 1024;
type GeometryEntry = { geometry: BufferGeometry; bytes: number };

function InstanceGroup({ geometry, instances, selectedId, onSelect, onOpenProperties, clippingPlanes, mesh, onFace }: {
  geometry: BufferGeometry; instances: SceneInstance[]; selectedId?: string | null; onSelect?: (id: string) => void; onOpenProperties?: () => void; clippingPlanes?: Plane[]; mesh: SceneMesh; onFace?: (instance: SceneInstance, faceIndex: number) => void;
}) {
  const ref = useRef<InstancedMesh>(null);
  useLayoutEffect(() => {
    if (!ref.current) return;
    instances.forEach((instance, index) => {
      ref.current!.setMatrixAt(index, new Matrix4().fromArray(instance.matrix).transpose());
      ref.current!.setColorAt(index, new Color(instance.feature_id === selectedId ? "#428578" : "#aeb8b2"));
    });
    ref.current.instanceMatrix.needsUpdate = true;
    if (ref.current.instanceColor) ref.current.instanceColor.needsUpdate = true;
    ref.current.computeBoundingSphere();
  }, [instances, selectedId]);
  return <instancedMesh ref={ref} args={[geometry, undefined, instances.length]}
    onDoubleClick={(e)=>{if(e.instanceId !== undefined){e.stopPropagation(); onOpenProperties?.();}}}
    onClick={(e) => { if (e.delta <= 3 && e.instanceId !== undefined) { e.stopPropagation(); const face = mesh.face_ranges?.find(range => e.faceIndex != null && e.faceIndex >= range.start && e.faceIndex < range.start + range.count);
      if (onFace && face) onFace(instances[e.instanceId], face.face_index); else onSelect?.(instances[e.instanceId].feature_id); } }}>
    <meshStandardMaterial color="white" metalness={0.08} roughness={0.72} clippingPlanes={clippingPlanes} clipShadows />
  </instancedMesh>;
}

export default function DocumentSceneViewer({ documentId, revisionId, selectedId, onSelect, onOpenProperties, fitRequest = 0, nativeDocument, onSelectTopology, comparison }: {
  documentId: string; revisionId: string; selectedId?: string | null; onSelect?: (id: string) => void; onOpenProperties?: () => void; fitRequest?: number; nativeDocument?: CloudDocument | null; onSelectTopology?: (id: string, selector: NonNullable<SelectionContext["topology_selector"]>) => void;
  comparison?:{id:string;pose:CameraPose|null;onPose:(pose:CameraPose)=>void;onBounds:(revision:string,bounds:{min:number[];max:number[]})=>void;bounds:Box3|null};
}) {
  const [selectionMode, setSelectionMode] = useState<"feature" | "face">("feature");
  const [selectionNotice, setSelectionNotice] = useState("");
  const [orthographic, setOrthographic] = useState(false);
  const [view, setView] = useState<StandardView>("isometric");
  const [viewRequest, setViewRequest] = useState(0);
  const [hidden, setHidden] = useState<string[]>([]);
  const [isolated, setIsolated] = useState<string | null>(null);
  const [section, setSection] = useState(false);
  const [selectedFit, setSelectedFit] = useState<number | null>(null);
  const [internalFit, setInternalFit] = useState(0);
  const contextRef = useRef<HTMLDivElement>(null);
  const [contextMenu,setContextMenu]=useState<{x:number;y:number}|null>(null);
  useEffect(()=>{
    if (!contextMenu) return;
    contextRef.current?.querySelector<HTMLButtonElement>('button')?.focus();
    const close=(event:PointerEvent)=>{if (!contextRef.current?.contains(event.target as Node)) setContextMenu(null);};
    window.addEventListener('pointerdown',close);
    return ()=>window.removeEventListener('pointerdown',close);
  },[contextMenu]);
  const [lod, setLOD] = useState<SceneLOD>("medium");
  const [loaded, setLoaded] = useState<{ scene: DocumentScene; lod: SceneLOD; bounds: Box3; geometries: Map<string, GeometryEntry> } | null>(null);
  const [error, setError] = useState("");
  const [pending, setPending] = useState(false);
  const [progress,setProgress] = useState("正在同步部件…");
  const [failedJob,setFailedJob] = useState<string | null>(null);
  const [retry,setRetry] = useState<{workflowId:string; documentId:string; revisionId:string; number:number} | null>(null);
  const [anchor, setAnchor] = useState<{ center: Vector3; size: Vector3 } | null>(null);
  const displayedGeometries = useRef(new Set<GeometryEntry>());
  useEffect(()=>{displayedGeometries.current = new Set(loaded?.geometries.values());},[loaded]);
  const cache = useRef(new Map<string, GeometryEntry>());
  useEffect(() => {
    const geometries = cache.current;
    return () => { for (const entry of geometries.values()) entry.geometry.dispose(); geometries.clear(); };
  }, []);
  useEffect(() => {
    let active = true;
    const controller = new AbortController();
    async function mesh(item: SceneMesh): Promise<GeometryEntry> {
      const existing = cache.current.get(item.sha256);
      if (existing) return existing;
      const response = await authFetch(API + item.url, { signal: controller.signal });
      if (!response.ok) throw new Error(`部件网格读取失败 (${response.status})`);
      const buffer = await response.arrayBuffer();
      const digest = Array.from(new Uint8Array(await crypto.subtle.digest("SHA-256", buffer)), (b) => b.toString(16).padStart(2, "0")).join("");
      if (buffer.byteLength !== item.size_bytes || digest !== item.sha256) throw new Error("部件网格完整性校验失败");
      if (!active) throw new DOMException("Aborted", "AbortError");
      const alreadyLoaded = cache.current.get(item.sha256);
      if (alreadyLoaded) return alreadyLoaded;
      const geometry = new STLLoader().parse(buffer);
      geometry.computeBoundingBox();
      const entry = { geometry, bytes: Object.values(geometry.attributes).reduce((sum, a) => sum + a.array.byteLength, 0) };
      cache.current.set(item.sha256, entry);
      return entry;
    }
    async function load() {
      setPending(true); setError(""); setFailedJob(null); setProgress("正在同步部件…");
      try {
        const scene = await readDocumentScene(documentId,revisionId,controller.signal,
          message => { if (active) setProgress(message); },retry?.documentId === documentId && retry.revisionId === revisionId ? retry.workflowId : undefined);
        const geometries = new Map<string, GeometryEntry>();
        const definitions = Object.entries(scene.definitions);
        for (let offset = 0; offset < definitions.length; offset += 4) {
          await Promise.all(definitions.slice(offset, offset + 4).map(async ([digest, definition]) => { geometries.set(digest, await mesh(definition.lods[lod])); }));
        }
        if (!active) return;
        const bounds = new Box3();
        for (const instance of scene.instances) bounds.union(geometries.get(instance.geometry_sha256)!.geometry.boundingBox!.clone()
          .applyMatrix4(new Matrix4().fromArray(instance.matrix).transpose()));
        setAnchor((previous) => previous || { center: bounds.getCenter(new Vector3()).negate(), size: bounds.getSize(new Vector3()) });
        setLoaded({ scene, lod, bounds, geometries });
        const used = new Set(definitions.map(([, d]) => d.lods[lod].sha256));
        let bytes = Array.from(cache.current.values()).reduce((sum, entry) => sum + entry.bytes, 0);
        for (const [key, entry] of cache.current) {
          if (bytes <= CACHE_BYTES) break;
          if (!used.has(key) && !displayedGeometries.current.has(entry)) { entry.geometry.dispose(); cache.current.delete(key); bytes -= entry.bytes; }
        }
      } catch (e) {
        if (active) {setError(e instanceof Error ? e.message : "部件场景读取失败"); if (e instanceof SceneJobError) setFailedJob(e.workflowId);}
      } finally { if (active) setPending(false); }
    }
    void load();
    return () => { active = false; controller.abort(); };
  }, [documentId, revisionId, lod, retry]);
  const current = loaded?.scene.document_id === documentId ? loaded : null;
  const composition = useMemo(() => {
    const groups = new Map<string, SceneInstance[]>();
    if (current) for (const instance of current.scene.instances) {
      if (hidden.includes(instance.feature_id) || isolated && instance.feature_id !== isolated) continue;
      groups.set(instance.geometry_sha256, [...(groups.get(instance.geometry_sha256) || []), instance]);
    }
    return groups;
  }, [current, hidden, isolated]);
  const bounds = useMemo(() => {
    if (!current || !anchor) return null;
    const selected = selectedFit === fitRequest ? current.scene.instances.filter(instance => instance.feature_id === selectedId) : Array.from(composition.values()).flat();
    if (!selected.length) return comparison ? current.bounds.clone() : current.bounds.clone().translate(anchor.center);
    const bounds = new Box3();
    for (const instance of selected) bounds.union(current.geometries.get(instance.geometry_sha256)!.geometry.boundingBox!.clone().applyMatrix4(new Matrix4().fromArray(instance.matrix).transpose()));
    return comparison ? bounds : bounds.translate(anchor.center);
  }, [current, anchor, selectedFit, selectedId, composition, comparison, fitRequest]);
  const clippingPlanes = useMemo(() => section && bounds ? [new Plane(new Vector3(0, 0, -1), bounds.getCenter(new Vector3()).z)] : [], [section, bounds]);
  const matchingRevision = current?.scene.revision_id === revisionId;
  const onComparisonBounds=comparison?.onBounds;
  useEffect(()=>{if (current?.scene.revision_id===revisionId && onComparisonBounds) onComparisonBounds(revisionId,{min:current.bounds.min.toArray(),max:current.bounds.max.toArray()});},[current,revisionId,onComparisonBounds]);
  const triangles = current ? current.scene.instances.reduce((sum, i) => sum + current.scene.definitions[i.geometry_sha256].lods[current.lod].triangles, 0) : 0;
  return <div className="relative flex h-full w-full min-w-0 flex-col bg-[#f4f4f1]" tabIndex={0} role="region" aria-label="原生三维视口" data-testid="document-scene" data-requested-revision={revisionId} data-revision={current?.scene.revision_id} data-lod={current?.lod}
    onKeyDown={event=>{if(event.key==='Escape')setContextMenu(null);if(event.ctrlKey || event.metaKey || event.altKey || (event.target as HTMLElement).matches('input,select,textarea,button,summary'))return;
      if(event.key.toLowerCase()==='f'){event.preventDefault();setSelectedFit(null);setInternalFit(v=>v+1);}
      if(['1','2','3'].includes(event.key)){event.preventDefault();setView(event.key==='1'?'front':event.key==='2'?'top':'isometric');setViewRequest(v=>v+1);}
      if(event.key.toLowerCase()==='p' && onOpenProperties && selectedId && matchingRevision){event.preventDefault();onOpenProperties();}
      if(event.key==='Escape')setContextMenu(null);}}
    onContextMenu={event=>{if(!(event.target instanceof HTMLCanvasElement))return;event.preventDefault();const rect=event.currentTarget.getBoundingClientRect();setContextMenu({x:Math.max(0,Math.min(event.clientX-rect.left,rect.width-210)),y:Math.max(0,Math.min(event.clientY-rect.top,rect.height-160))});}}>
    <div className="ww-scene-toolbar">
      <label>显示精度 <select aria-label="网格精度" value={lod} onChange={(e) => setLOD(e.target.value as SceneLOD)}>
        <option value="coarse">快速</option><option value="medium">标准</option><option value="fine">精细</option>
      </select></label>
      <label>选择 <select aria-label="选择模式" value={selectionMode} onChange={event => { setSelectionMode(event.target.value as typeof selectionMode); setSelectionNotice(""); }}><option value="feature">部件</option><option value="face">已验证平面</option></select></label>
      <label>视图 <select aria-label="标准视图" value={view} onChange={event => { setView(event.target.value as StandardView); setViewRequest(value => value + 1); }}>
        {([['isometric','等轴测'],['front','前视'],['back','后视'],['top','俯视'],['bottom','仰视'],['left','左视'],['right','右视']] as const).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
      </select></label>
      <button type="button" className="workspace-button" disabled={Boolean(comparison)} aria-pressed={orthographic} onClick={() => setOrthographic(value => !value)}>正交</button>
      <details className="ww-view-actions"><summary>查看工具</summary><div className="flex flex-wrap gap-2">
        <button type="button" className="workspace-button" disabled={!matchingRevision || !selectedId || !current?.scene.instances.some(instance => instance.feature_id === selectedId)} onClick={() => { setSelectedFit(fitRequest); setInternalFit(value => value + 1); }}>适配选中</button>
        <button type="button" className="workspace-button" disabled={!matchingRevision || !selectedId || !current?.scene.instances.some(instance => instance.feature_id === selectedId)} onClick={() => setHidden(previous => [...new Set([...previous, selectedId!])])}>隐藏选中</button>
        <button type="button" className="workspace-button" aria-pressed={Boolean(isolated)} disabled={!matchingRevision || !selectedId || !current?.scene.instances.some(instance => instance.feature_id === selectedId)} onClick={() => setIsolated(selectedId!)}>隔离选中</button>
        <button type="button" className="workspace-button" onClick={() => { setHidden([]); setIsolated(null); setSelectedFit(null); }}>显示全部</button>
        <button type="button" className="workspace-button" aria-pressed={section} onClick={() => setSection(value => !value)}>剖切查看</button>
      </div></details>
      {current ? <p>{current.scene.instances.length} 个部件 · {current.geometries.size} 份几何 · {triangles.toLocaleString()} 个三角形</p> : null}
      {selectedId && current && !current.scene.instances.some(instance => instance.feature_id === selectedId)
        ? <p role="status">已选中特征；此特征没有独立部件网格，无法单独高亮。</p> : null}
    </div>
    {selectionNotice ? <p role="status" className="px-3 type-caption">{selectionNotice}</p> : null}
    {pending ? <p role="status" className="absolute bottom-3 left-3 z-10 rounded bg-white/95 p-2 type-caption">{current ? "正在更新几何，暂时显示上一画面 · " : ""}{progress}</p> : null}
    {error ? <p role="alert" className="absolute bottom-3 left-3 z-10 rounded bg-white/95 p-2 type-caption text-red-700">{error}{current && !matchingRevision ? "；当前保留上一版本画面，尚未显示所请求版本。" : ""}</p> : null}
    {failedJob ? <button type="button" className="workspace-button absolute bottom-14 left-3 z-10 bg-white"
      onClick={() => setRetry(previous => ({workflowId:failedJob,documentId,revisionId,number:(previous?.number || 0) + 1}))}>重新计算场景</button> : null}
    <div className="min-h-0 min-w-0 flex-1"><Canvas onCreated={({ gl }) => { gl.localClippingEnabled = true; }} events={canvasEvents} camera={{ fov: 50, position: [100, 100, 100] }} fallback={<p role="status">当前浏览器无法创建 3D 画布，请启用硬件加速后重试。</p>}>
      <color args={["#f4f4f1"]} attach="background" />
      <ambientLight intensity={1.1} /><directionalLight intensity={1.4} position={[10, 10, 5]} />
      <directionalLight intensity={0.5} position={[-10, -10, -5]} />
      <CameraFit bounds={comparison?.bounds || bounds} request={fitRequest + internalFit} />
      {comparison ? <CameraLink id={comparison.id} pose={comparison.pose} onPose={comparison.onPose}/> : null}
      <ViewControls orthographic={orthographic} view={view} request={viewRequest} />
      <group position={comparison ? [0,0,0] : anchor?.center}>
        {current && Array.from(composition, ([digest, instances]) => <InstanceGroup key={`${digest}:${current.lod}`}
          geometry={current.geometries.get(digest)!.geometry} instances={instances} selectedId={matchingRevision ? selectedId : null} onSelect={matchingRevision ? onSelect : undefined} onOpenProperties={matchingRevision ? onOpenProperties : undefined} clippingPlanes={clippingPlanes} mesh={current.scene.definitions[digest].lods[current.lod]} onFace={matchingRevision && selectionMode === "face" ? (instance, faceIndex) => {
            const mapped = instance.face_bindings?.find(binding => binding.face_index === faceIndex);
            const selector = nativeDocument?.features.find(feature => feature.id === instance.feature_id)?.topology_bindings?.find(binding => binding.subelement_kind === "face" && binding.axis === mapped?.axis && binding.extreme === mapped?.extreme && binding.revision_id === revisionId);
            if (selector && onSelectTopology) { onSelectTopology(instance.feature_id, selector); setSelectionNotice(""); }
            else setSelectionNotice("此面没有完整且唯一的当前修订拓扑依据；请选择其他已验证平面或切换部件模式。");
          } : undefined} />)}
      </group>
      <OrbitControls enableDamping makeDefault />
      <Grid rotation={[Math.PI / 2, 0, 0]} position={[0, 0, comparison ? 0 : anchor?.center.z || 0]} cellColor="#d8dad5" cellSize={10} fadeDistance={400} infiniteGrid sectionColor="#b7bab3" sectionSize={50} />
    </Canvas></div>
    {contextMenu?<div ref={contextRef} role="menu" aria-label="视口命令" className="absolute z-30 grid w-[210px] gap-1 rounded border border-[var(--line)] bg-white p-2 shadow-lg" style={{left:contextMenu.x,top:contextMenu.y}}><button type="button" role="menuitem" className="workspace-button" onClick={()=>{setSelectedFit(null);setInternalFit(v=>v+1);setContextMenu(null);}}>适应视图 · F</button><button type="button" role="menuitem" className="workspace-button" onClick={()=>{setView('isometric');setViewRequest(v=>v+1);setContextMenu(null);}}>等轴测 · 3</button>{onOpenProperties?<button type="button" role="menuitem" className="workspace-button" disabled={!selectedId || !matchingRevision} onClick={()=>{onOpenProperties();setContextMenu(null);}}>属性与测量 · P</button>:null}<button type="button" role="menuitem" className="workspace-button" onClick={()=>setContextMenu(null)}>关闭菜单 · Esc</button></div>:null}
  </div>;
}
