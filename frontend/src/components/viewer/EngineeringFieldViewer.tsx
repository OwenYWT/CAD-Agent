import { Bounds, OrbitControls } from '@react-three/drei';
import { Canvas } from '@react-three/fiber';
import { useEffect, useMemo, useState } from 'react';
import { BufferGeometry, Float32BufferAttribute, DoubleSide, Color } from 'three';
import type { EngineeringField } from '../../types/engineeringTask';

export default function EngineeringFieldViewer({field}: {field: EngineeringField}) {
  const [metric, setMetric] = useState<'stress' | 'displacement'>('stress');
  const [scale, setScale] = useState(0);
  const result = useMemo(() => {
    const values = metric === 'stress' ? field.von_mises_mpa : field.displacements_mm.map(v => Math.hypot(...v));
    const maximum = Math.max(...values), geometry = new BufferGeometry();
    const colors: number[] = [], color = new Color();
    geometry.setAttribute('position', new Float32BufferAttribute(field.positions_mm.flatMap((p,i) => p.map((v,j) => v + field.displacements_mm[i][j] * scale)), 3));
    for (const value of values) { color.setHSL((1-(maximum ? value/maximum : 0))*0.66, 0.8, 0.48); colors.push(color.r,color.g,color.b); }
    geometry.setAttribute('color', new Float32BufferAttribute(colors,3)); geometry.setIndex(field.triangles.flat()); geometry.computeVertexNormals();
    return {geometry, maximum};
  }, [field, metric, scale]);
  useEffect(() => () => result.geometry.dispose(), [result]);
  return <div data-testid="engineering-field-viewer">
    <label className="block type-caption">结果显示<select aria-label="有限元显示量" value={metric} onChange={e => setMetric(e.target.value as typeof metric)} className="ml-2 border p-1">
      <option value="stress">von Mises 应力 / MPa</option><option value="displacement">位移 / mm</option></select></label>
    <div className="my-2 h-64 rounded border border-[var(--line)] bg-[var(--surface)]" aria-label="有限元结果三维视图">
      <Canvas camera={{position:[100,-100,100],up:[0,0,1]}} frameloop="demand">
        <ambientLight intensity={1.5} /><directionalLight position={[5,-10,20]} intensity={1.5} />
        <OrbitControls makeDefault /><Bounds fit clip observe margin={1.3}><mesh geometry={result.geometry}>
          <meshStandardMaterial vertexColors side={DoubleSide} roughness={0.9} /></mesh></Bounds>
      </Canvas>
    </div>
    <div className="h-2 rounded" style={{background:'linear-gradient(to right,#163edc,#21b997,#e9d91a,#dc1616)'}} />
    <div className="flex justify-between type-caption"><span>0</span><span data-testid="engineering-field-maximum">{result.maximum.toPrecision(5)} {metric==='stress' ? 'MPa' : 'mm'}</span></div>
    <label className="mt-2 block type-caption">形变显示倍率 <output>{scale}×</output><input className="w-full" aria-label="形变显示倍率" type="range" min={0} max={100} step={1} value={scale} onChange={e=>setScale(Number(e.target.value))} /></label>
    <p className="type-caption text-[var(--muted)]">倍率仅放大显示；色标始终使用原始计算值。</p>
  </div>;
}
