import { Canvas } from "@react-three/fiber";
import { OrbitControls, Grid } from "@react-three/drei";
import { useCADModel } from "../hooks/useCADModel";
import ModelAnnotations from "./ModelAnnotations";
import type { Annotation3D } from "../types";

function CADMesh({ stlUrl }: { stlUrl: string }) {
  const { geometry, isLoading } = useCADModel(stlUrl);

  if (isLoading || !geometry) return null;

  return (
    <mesh geometry={geometry}>
      <meshStandardMaterial
        color="#4a9eff"
        metalness={0.3}
        roughness={0.6}
      />
    </mesh>
  );
}

function Scene({
  stlUrl,
  annotations,
  showAnnotations,
  selectedAnnotation,
  onSelectAnnotation,
}: {
  stlUrl: string;
  annotations: Annotation3D[];
  showAnnotations: boolean;
  selectedAnnotation: string | null;
  onSelectAnnotation: (id: string | null) => void;
}) {
  const { centerOffset } = useCADModel(stlUrl);

  return (
    <>
      <CADMesh stlUrl={stlUrl} />
      <ModelAnnotations
        annotations={annotations}
        centerOffset={centerOffset}
        selectedId={selectedAnnotation}
        onSelect={onSelectAnnotation}
        visible={showAnnotations}
      />
    </>
  );
}

function LoadingOverlay() {
  return (
    <div className="absolute inset-0 flex items-center justify-center z-10 pointer-events-none">
      <div className="bg-black/50 text-white px-4 py-2 rounded-lg text-sm flex items-center gap-2">
        <span className="inline-block w-4 h-4 border-2 border-white border-t-transparent rounded-full animate-spin" />
        加载模型中...
      </div>
    </div>
  );
}

interface Viewer3DProps {
  stlUrl: string | null;
  annotations?: Annotation3D[];
  showAnnotations?: boolean;
  selectedAnnotation?: string | null;
  onSelectAnnotation?: (id: string | null) => void;
  onToggleAnnotations?: () => void;
}

export default function Viewer3D({
  stlUrl,
  annotations = [],
  showAnnotations = false,
  selectedAnnotation = null,
  onSelectAnnotation,
  onToggleAnnotations,
}: Viewer3DProps) {
  const { isLoading } = useCADModel(stlUrl);

  if (!stlUrl) {
    return (
      <div className="w-full h-full bg-gradient-to-b from-gray-50 to-gray-100 overflow-hidden relative flex items-center justify-center">
        <div className="text-center space-y-3">
          <div className="text-5xl opacity-20">📦</div>
          <p className="text-sm text-gray-400">
            3D 模型将在此处显示
          </p>
        </div>
      </div>
    );
  }

  return (
    <div className="w-full h-full bg-gray-900 rounded-lg overflow-hidden relative">
      {isLoading && <LoadingOverlay />}

      {/* Annotation toggle */}
      {annotations.length > 0 && (
        <div className="absolute top-2 right-2 z-10 flex items-center gap-1.5 bg-black/60 rounded px-2 py-1">
          <label className="text-xs text-white/80 cursor-pointer select-none flex items-center gap-1">
            <input
              type="checkbox"
              checked={showAnnotations}
              onChange={() => onToggleAnnotations?.()}
              className="w-3 h-3"
            />
            标注 ({annotations.length})
          </label>
        </div>
      )}

      <Canvas camera={{ position: [150, 150, 150], fov: 50 }}>
        <ambientLight intensity={0.4} />
        <directionalLight position={[10, 10, 5]} intensity={0.8} />
        <directionalLight position={[-10, -10, -5]} intensity={0.4} />
        <Scene
          stlUrl={stlUrl}
          annotations={annotations}
          showAnnotations={showAnnotations}
          selectedAnnotation={selectedAnnotation}
          onSelectAnnotation={onSelectAnnotation || (() => {})}
        />
        <OrbitControls enableDamping />
        <Grid
          infiniteGrid
          cellSize={10}
          sectionSize={50}
          fadeDistance={400}
          cellColor="#333333"
          sectionColor="#555555"
        />
      </Canvas>
    </div>
  );
}
