import { Grid, OrbitControls } from "@react-three/drei";
import { Canvas } from "@react-three/fiber";
import { useState } from "react";
import type { BufferGeometry } from "three";
import { useCADModel } from "../hooks/useCADModel";
import type { Annotation3D } from "../types";
import ModelAnnotations from "./ModelAnnotations";
import { Icon } from "./ui/Icon";

interface Viewer3DProps {
  stlUrl: string | null;
  annotations?: Annotation3D[];
  showAnnotations?: boolean;
  selectedAnnotation?: string | null;
  onSelectAnnotation?: (id: string | null) => void;
  onToggleAnnotations?: () => void;
}

function useWebGLAvailability() {
  const [available] = useState(() => {
    const canvas = document.createElement("canvas");
    try {
      const context = canvas.getContext("webgl2")
        || canvas.getContext("webgl");
      const isAvailable = Boolean(context);
      context
        ?.getExtension("WEBGL_lose_context")
        ?.loseContext();
      return isAvailable;
    } catch {
      return false;
    }
  });

  return available;
}

function Scene({
  geometry,
  annotations,
  centerOffset,
  showAnnotations,
  selectedAnnotation,
  onSelectAnnotation,
}: {
  geometry: BufferGeometry;
  annotations: Annotation3D[];
  centerOffset: [number, number, number];
  showAnnotations: boolean;
  selectedAnnotation: string | null;
  onSelectAnnotation: (id: string | null) => void;
}) {
  return (
    <>
      <mesh geometry={geometry}>
        <meshStandardMaterial color="#aeb8b2" metalness={0.08} roughness={0.72} />
      </mesh>
      <ModelAnnotations annotations={annotations} centerOffset={centerOffset} onSelect={onSelectAnnotation} selectedId={selectedAnnotation} visible={showAnnotations} />
      <OrbitControls enableDamping makeDefault />
      <Grid cellColor="#d8dad5" cellSize={10} fadeDistance={400} infiniteGrid sectionColor="#b7bab3" sectionSize={50} />
    </>
  );
}

function LoadedViewer({
  stlUrl,
  annotations,
  showAnnotations,
  selectedAnnotation,
  onSelectAnnotation,
  onToggleAnnotations,
}: {
  stlUrl: string;
  annotations: Annotation3D[];
  showAnnotations: boolean;
  selectedAnnotation: string | null;
  onSelectAnnotation?: (id: string | null) => void;
  onToggleAnnotations?: () => void;
}) {
  const { geometry, isLoading, error, centerOffset } = useCADModel(stlUrl);
  const webGLAvailable = useWebGLAvailability();

  return (
    <div className="relative h-full w-full overflow-hidden bg-[#f4f4f1]">
      {isLoading && (
        <div className="pointer-events-none absolute inset-0 z-10 flex items-center justify-center" role="status">
          <div className="flex items-center gap-2 rounded-md border border-slate-200 bg-white/90 px-4 py-2 text-sm text-slate-700 shadow-lg">
            <span className="h-4 w-4 animate-spin rounded-full border-2 border-slate-500 border-t-transparent" />
            正在加载模型
          </div>
        </div>
      )}

      {error ? (
        <div className="flex h-full items-center justify-center px-6 text-center" role="alert">
          <div className="max-w-sm">
            <Icon className="mx-auto text-red-400" name="box" size={32} />
            <p className="mt-3 text-sm font-medium text-red-700">3D 模型加载失败</p>
            <p className="mt-1 text-xs leading-5 text-slate-500">模型文件可能已过期或不可访问，请重新生成后再试。</p>
          </div>
        </div>
      ) : webGLAvailable === false ? (
        <div className="flex h-full items-center justify-center px-6 text-center" role="status">
          <div className="max-w-md">
            <Icon className="mx-auto text-amber-500" name="box" size={32} />
            <p className="mt-3 text-sm font-medium text-slate-700">当前浏览器无法创建 3D 画布</p>
            <p className="mt-1 text-xs leading-5 text-slate-500">
              {geometry
                ? "STL 文件已成功读取，但 WebGL 不可用。请启用浏览器硬件加速后重新打开模型。"
                : "正在验证 STL 文件；请启用浏览器硬件加速后重新打开模型。"}
            </p>
          </div>
        </div>
      ) : (
        <>
          {annotations.length > 0 && (
            <button
              aria-checked={showAnnotations}
              aria-label="显示工程标注"
              className="absolute right-3 top-3 z-10 flex min-h-11 items-center gap-2 rounded-md border border-slate-200 bg-white/90 px-3 text-xs font-medium text-slate-700 shadow-lg backdrop-blur-sm"
              onClick={onToggleAnnotations}
              role="switch"
              title="显示工程标注"
              type="button"
            >
              <span className={`relative h-5 w-9 rounded-full transition-colors ${showAnnotations ? "bg-emerald-600" : "bg-slate-300"}`}>
                <span className={`absolute top-0.5 h-4 w-4 rounded-full bg-white transition-transform ${showAnnotations ? "translate-x-[18px]" : "translate-x-0.5"}`} />
              </span>
              工程标注 {annotations.length}
            </button>
          )}
          <Canvas camera={{ fov: 50, position: [150, 150, 150] }}>
            <color args={["#f4f4f1"]} attach="background" />
            <ambientLight intensity={1.1} />
            <directionalLight intensity={1.4} position={[10, 10, 5]} />
            <directionalLight intensity={0.5} position={[-10, -10, -5]} />
            {geometry && (
              <Scene
                annotations={annotations}
                centerOffset={centerOffset}
                geometry={geometry}
                onSelectAnnotation={onSelectAnnotation || (() => {})}
                selectedAnnotation={selectedAnnotation}
                showAnnotations={showAnnotations}
              />
            )}
          </Canvas>
        </>
      )}
    </div>
  );
}

export default function Viewer3D({
  stlUrl,
  annotations = [],
  showAnnotations = false,
  selectedAnnotation = null,
  onSelectAnnotation,
  onToggleAnnotations,
}: Viewer3DProps) {
  if (!stlUrl) {
    return (
      <div className="flex h-full w-full items-center justify-center overflow-hidden bg-[#f4f4f1] px-6 text-center">
        <div>
          <Icon className="mx-auto text-slate-300" name="box" size={42} />
          <p className="mt-3 text-sm font-medium text-slate-600">3D 模型将在生成后显示</p>
          <p className="mt-1 text-xs text-slate-400">拖动旋转，滚轮缩放，右键平移</p>
        </div>
      </div>
    );
  }

  return (
    <LoadedViewer
      annotations={annotations}
      key={stlUrl}
      onSelectAnnotation={onSelectAnnotation}
      onToggleAnnotations={onToggleAnnotations}
      selectedAnnotation={selectedAnnotation}
      showAnnotations={showAnnotations}
      stlUrl={stlUrl}
    />
  );
}
