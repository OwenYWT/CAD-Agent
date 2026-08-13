import { Grid, OrbitControls } from "@react-three/drei";
import { Canvas } from "@react-three/fiber";
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

  return (
    <div className="relative h-full w-full overflow-hidden bg-[#f4f4f1]">
      {isLoading ? (
        <div className="pointer-events-none absolute inset-0 z-10 flex items-center justify-center" role="status">
          <div className="flex items-center gap-2 rounded-md border border-slate-200 bg-white/90 px-4 py-2 text-sm text-slate-700 shadow-lg">
            <span className="h-4 w-4 animate-spin rounded-full border-2 border-slate-500 border-t-transparent" />
            {String.fromCharCode(0x6b63, 0x5728, 0x52a0, 0x8f7d, 0x6a21, 0x578b)}
          </div>
        </div>
      ) : null}

      {error ? (
        <div className="flex h-full items-center justify-center px-6 text-center" role="alert">
          <div className="max-w-sm">
            <Icon className="mx-auto text-red-400" name="box" size={32} />
            <p className="mt-3 text-sm font-medium text-red-700">{String.fromCharCode(0x33, 0x44, 0x20, 0x6a21, 0x578b, 0x52a0, 0x8f7d, 0x5931, 0x8d25)}</p>
            <p className="mt-1 text-xs leading-5 text-slate-500">{String.fromCharCode(0x6a21, 0x578b, 0x6587, 0x4ef6, 0x53ef, 0x80fd, 0x5df2, 0x8fc7, 0x671f, 0x6216, 0x4e0d, 0x53ef, 0x8bbf, 0x95ee, 0xff0c, 0x8bf7, 0x91cd, 0x65b0, 0x751f, 0x6210, 0x540e, 0x518d, 0x8bd5, 0x3002)}</p>
          </div>
        </div>
      ) : (
        <>
          {annotations.length > 0 ? (
            <button
              aria-checked={showAnnotations}
              aria-label={String.fromCharCode(0x663e, 0x793a, 0x5de5, 0x7a0b, 0x6807, 0x6ce8)}
              className="absolute right-3 top-3 z-10 flex min-h-11 items-center gap-2 rounded-md border border-slate-200 bg-white/90 px-3 text-xs font-medium text-slate-700 shadow-lg backdrop-blur-sm"
              onClick={onToggleAnnotations}
              role="switch"
              title={String.fromCharCode(0x663e, 0x793a, 0x5de5, 0x7a0b, 0x6807, 0x6ce8)}
              type="button"
            >
              <span className={`relative h-5 w-9 rounded-full transition-colors ${showAnnotations ? "bg-emerald-600" : "bg-slate-300"}`}>
                <span className={`absolute top-0.5 h-4 w-4 rounded-full bg-white transition-transform ${showAnnotations ? "translate-x-[18px]" : "translate-x-0.5"}`} />
              </span>
              {String.fromCharCode(0x5de5, 0x7a0b, 0x6807, 0x6ce8)} {annotations.length}
            </button>
          ) : null}
          <Canvas className="absolute inset-0" camera={{ fov: 50, position: [150, 150, 150] }}>
            <color args={["#f4f4f1"]} attach="background" />
            <ambientLight intensity={1.1} />
            <directionalLight intensity={1.4} position={[10, 10, 5]} />
            <directionalLight intensity={0.5} position={[-10, -10, -5]} />
            {geometry ? (
              <Scene
                annotations={annotations}
                centerOffset={centerOffset}
                geometry={geometry}
                onSelectAnnotation={onSelectAnnotation || (() => {})}
                selectedAnnotation={selectedAnnotation}
                showAnnotations={showAnnotations}
              />
            ) : null}
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
          <p className="mt-3 text-sm font-medium text-slate-600">{String.fromCharCode(0x33, 0x44, 0x20, 0x6a21, 0x578b, 0x5c06, 0x5728, 0x751f, 0x6210, 0x540e, 0x663e, 0x793a)}</p>
          <p className="mt-1 text-xs text-slate-400">{String.fromCharCode(0x62d6, 0x52a8, 0x65cb, 0x8f6c, 0xff0c, 0x6eda, 0x8f6e, 0x7f29, 0x653e, 0xff0c, 0x53f3, 0x952e, 0x5e73, 0x79fb)}</p>
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
