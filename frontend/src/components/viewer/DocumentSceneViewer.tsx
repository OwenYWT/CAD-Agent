import { Grid, OrbitControls } from "@react-three/drei";
import { Canvas, useThree } from "@react-three/fiber";
import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { Box3, BufferGeometry, Color, InstancedMesh, Matrix4, PerspectiveCamera, Vector3 } from "three";
import { STLLoader } from "three/examples/jsm/loaders/STLLoader.js";
import { authFetch } from "../../auth";
import type { DocumentScene, SceneInstance, SceneLOD, SceneMesh } from "../../types/scene";

const API = import.meta.env.VITE_API_BASE || "";
const CACHE_BYTES = 128 * 1024 * 1024;
type GeometryEntry = { geometry: BufferGeometry; bytes: number };

function InitialCameraFit({ size }: { size: Vector3 | null }) {
  const getRendererState = useThree((state) => state.get);
  const fitted = useRef(false);
  useLayoutEffect(() => {
    const camera = getRendererState().camera;
    if (!size || fitted.current || !(camera instanceof PerspectiveCamera)) return;
    const vertical = camera.fov * Math.PI / 180;
    const horizontal = 2 * Math.atan(Math.tan(vertical / 2) * camera.aspect);
    const distance = Math.max(size.length(), 1) / (2 * Math.sin(Math.min(vertical, horizontal) / 2)) * 1.15;
    camera.up.set(0, 0, 1);
    camera.position.copy(new Vector3(1, -1, 1).normalize().multiplyScalar(distance));
    camera.near = Math.max(distance / 10000, 0.001); camera.far = distance * 100;
    camera.lookAt(0, 0, 0); camera.updateProjectionMatrix(); fitted.current = true;
  }, [getRendererState, size]);
  return null;
}

function InstanceGroup({ geometry, instances, selectedId, onSelect }: {
  geometry: BufferGeometry; instances: SceneInstance[]; selectedId?: string | null; onSelect?: (id: string) => void;
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
    onClick={(e) => { if (e.instanceId !== undefined) { e.stopPropagation(); onSelect?.(instances[e.instanceId].feature_id); } }}>
    <meshStandardMaterial color="white" metalness={0.08} roughness={0.72} />
  </instancedMesh>;
}

export default function DocumentSceneViewer({ documentId, revisionId, selectedId, onSelect }: {
  documentId: string; revisionId: string; selectedId?: string | null; onSelect?: (id: string) => void;
}) {
  const [lod, setLOD] = useState<SceneLOD>("medium");
  const [loaded, setLoaded] = useState<{ scene: DocumentScene; lod: SceneLOD; geometries: Map<string, GeometryEntry> } | null>(null);
  const [error, setError] = useState("");
  const [pending, setPending] = useState(false);
  const [anchor, setAnchor] = useState<{ center: Vector3; size: Vector3 } | null>(null);
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
      setPending(true); setError("");
      try {
        const response = await authFetch(`${API}/api/documents/${documentId}/scenes/${revisionId}`, { signal: controller.signal });
        if (!response.ok) throw new Error(`部件场景读取失败 (${response.status})`);
        const scene = await response.json() as DocumentScene;
        if (scene.schema_version !== "cad-scene.v1" || scene.document_id !== documentId || scene.revision_id !== revisionId) throw new Error("部件场景版本不匹配");
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
        setLoaded({ scene, lod, geometries });
        const used = new Set(definitions.map(([, d]) => d.lods[lod].sha256));
        let bytes = Array.from(cache.current.values()).reduce((sum, entry) => sum + entry.bytes, 0);
        for (const [key, entry] of cache.current) {
          if (bytes <= CACHE_BYTES) break;
          if (!used.has(key)) { entry.geometry.dispose(); cache.current.delete(key); bytes -= entry.bytes; }
        }
      } catch (e) {
        if (active) setError(e instanceof Error ? e.message : "部件场景读取失败");
      } finally { if (active) setPending(false); }
    }
    void load();
    return () => { active = false; controller.abort(); };
  }, [documentId, revisionId, lod]);
  const composition = useMemo(() => {
    const groups = new Map<string, SceneInstance[]>();
    if (loaded) for (const instance of loaded.scene.instances) {
      groups.set(instance.geometry_sha256, [...(groups.get(instance.geometry_sha256) || []), instance]);
    }
    return groups;
  }, [loaded]);
  const triangles = loaded ? loaded.scene.instances.reduce((sum, i) => sum + loaded.scene.definitions[i.geometry_sha256].lods[loaded.lod].triangles, 0) : 0;
  return <div className="relative h-full w-full bg-[#f4f4f1]" data-testid="document-scene" data-revision={loaded?.scene.revision_id} data-lod={loaded?.lod}>
    <div className="absolute right-3 top-3 z-10 rounded border border-[var(--line)] bg-white/95 p-2 type-caption">
      <label>显示精度 <select aria-label="网格精度" value={lod} onChange={(e) => setLOD(e.target.value as SceneLOD)}>
        <option value="coarse">快速</option><option value="medium">标准</option><option value="fine">精细</option>
      </select></label>
      {loaded ? <p>{loaded.scene.instances.length} 个部件 · {loaded.geometries.size} 份几何 · {triangles.toLocaleString()} 个三角形</p> : null}
    </div>
    {pending ? <p role="status" className="absolute bottom-3 left-3 z-10 rounded bg-white/95 p-2 type-caption">正在同步部件…</p> : null}
    {error ? <p role="alert" className="absolute bottom-3 left-3 z-10 rounded bg-white/95 p-2 type-caption text-red-700">{loaded && loaded.scene.revision_id !== revisionId ? "仍显示上一版本。" : ""}{error}</p> : null}
    <Canvas camera={{ fov: 50, position: [100, 100, 100] }} fallback={<p role="status">当前浏览器无法创建 3D 画布，请启用硬件加速后重试。</p>}>
      <color args={["#f4f4f1"]} attach="background" />
      <ambientLight intensity={1.1} /><directionalLight intensity={1.4} position={[10, 10, 5]} />
      <directionalLight intensity={0.5} position={[-10, -10, -5]} />
      <InitialCameraFit size={anchor?.size || null} />
      <group position={anchor?.center}>
        {loaded && Array.from(composition, ([digest, instances]) => <InstanceGroup key={`${digest}:${loaded.lod}`}
          geometry={loaded.geometries.get(digest)!.geometry} instances={instances} selectedId={selectedId} onSelect={onSelect} />)}
      </group>
      <OrbitControls enableDamping makeDefault />
      <Grid rotation={[Math.PI / 2, 0, 0]} position={[0, 0, anchor?.center.z || 0]} cellColor="#d8dad5" cellSize={10} fadeDistance={400} infiniteGrid sectionColor="#b7bab3" sectionSize={50} />
    </Canvas>
  </div>;
}
