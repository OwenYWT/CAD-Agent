import { useMemo } from "react";
import { Html } from "@react-three/drei";
import type { Annotation3D } from "../types";

const SEVERITY_COLOR: Record<string, string> = {
  critical: "#ef4444",
  warning: "#f59e0b",
  info: "#3b82f6",
};

const SEVERITY_BG: Record<string, string> = {
  critical: "rgba(239,68,68,0.15)",
  warning: "rgba(245,158,11,0.15)",
  info: "rgba(59,130,246,0.15)",
};

const SEVERITY_BORDER: Record<string, string> = {
  critical: "rgba(239,68,68,0.6)",
  warning: "rgba(245,158,11,0.6)",
  info: "rgba(59,130,246,0.6)",
};

interface AnnotationMarkerProps {
  annotation: Annotation3D;
  offset: [number, number, number];
  selected: boolean;
  onSelect: (id: string) => void;
}

function AnnotationMarker({ annotation, offset, selected, onSelect }: AnnotationMarkerProps) {
  const pos: [number, number, number] = [
    annotation.position[0] + offset[0],
    annotation.position[1] + offset[1],
    annotation.position[2] + offset[2],
  ];

  const color = SEVERITY_COLOR[annotation.severity] || SEVERITY_COLOR.info;
  const scale = selected ? 1.8 : 1;

  return (
    <group position={pos}>
      {/* 3D sphere marker */}
      <mesh
        scale={[scale, scale, scale]}
        onClick={(e) => {
          e.stopPropagation();
          onSelect(annotation.id);
        }}
      >
        <sphereGeometry args={[1.5, 16, 16]} />
        <meshBasicMaterial
          color={color}
          transparent
          opacity={selected ? 1 : 0.8}
        />
      </mesh>

      {/* Pulsing ring for critical */}
      {annotation.severity === "critical" && (
        <mesh scale={[2.5, 2.5, 2.5]}>
          <ringGeometry args={[1.0, 1.3, 24]} />
          <meshBasicMaterial
            color={color}
            transparent
            opacity={0.4}
            side={2}
          />
        </mesh>
      )}

      {/* HTML label */}
      <Html
        distanceFactor={200}
        center={false}
        style={{
          pointerEvents: "auto",
          transform: "translate(8px, -50%)",
          whiteSpace: "nowrap",
        }}
      >
        <div
          onClick={(e) => {
            e.stopPropagation();
            onSelect(annotation.id);
          }}
          style={{
            background: selected ? SEVERITY_BG[annotation.severity] : "rgba(0,0,0,0.75)",
            border: `1px solid ${selected ? SEVERITY_BORDER[annotation.severity] : "rgba(255,255,255,0.2)"}`,
            borderRadius: 4,
            padding: "2px 6px",
            fontSize: 11,
            color: selected ? color : "#fff",
            cursor: "pointer",
            maxWidth: 160,
            overflow: "hidden",
            textOverflow: "ellipsis",
            fontFamily: "system-ui, sans-serif",
            userSelect: "none",
            lineHeight: "16px",
          }}
        >
          {annotation.label}
        </div>
      </Html>
    </group>
  );
}

interface ModelAnnotationsProps {
  annotations: Annotation3D[];
  centerOffset: [number, number, number];
  selectedId: string | null;
  onSelect: (id: string | null) => void;
  visible: boolean;
}

export default function ModelAnnotations({
  annotations,
  centerOffset,
  selectedId,
  onSelect,
  visible,
}: ModelAnnotationsProps) {
  const filtered = useMemo(
    () => (visible ? annotations : []),
    [annotations, visible],
  );

  if (filtered.length === 0) return null;

  return (
    <group>
      {filtered.map((a) => (
        <AnnotationMarker
          key={a.id}
          annotation={a}
          offset={centerOffset}
          selected={selectedId === a.id}
          onSelect={(id) => onSelect(selectedId === id ? null : id)}
        />
      ))}
    </group>
  );
}
