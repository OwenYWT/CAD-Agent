import { Bounds, OrbitControls } from '@react-three/drei';
import { Canvas } from '@react-three/fiber';
import { useEffect,useMemo,useState } from 'react';
import { BufferGeometry,Float32BufferAttribute,DoubleSide } from 'three';
import type { CamToolpath } from '../../types/engineeringTask';

export default function CamToolpathViewer({field,cuttingLength}: {field:CamToolpath;cuttingLength:number}) {
  const [cursor,setCursor]=useState(field.trajectory.length-1),[playing,setPlaying]=useState(false);
  const isPlaying=playing && cursor<field.trajectory.length-1;
  const target=useMemo(()=>{const g=new BufferGeometry();g.setAttribute('position',new Float32BufferAttribute(field.positions_mm.flat(),3));g.setIndex(field.triangles.flat());g.computeVertexNormals();return g;},[field]);
  const paths=useMemo(()=>{
    const values={feed:[] as number[],rapid:[] as number[]};
    for (let i=1;i<=cursor;i++) values[field.trajectory[i].motion].push(...field.trajectory[i-1].position_mm,...field.trajectory[i].position_mm);
    return Object.fromEntries(Object.entries(values).map(([key,value])=>{const g=new BufferGeometry();g.setAttribute('position',new Float32BufferAttribute(value,3));return [key,g];})) as Record<'feed'|'rapid',BufferGeometry>;
  },[field,cursor]);
  useEffect(()=>()=>target.dispose(),[target]);
  useEffect(()=>()=>{paths.feed.dispose();paths.rapid.dispose();},[paths]);
  useEffect(()=>{
    if (!isPlaying) return;
    const timer=setInterval(()=>setCursor(value=>Math.min(field.trajectory.length-1,value+Math.max(1,Math.ceil(field.trajectory.length/100)))),100);
    return ()=>clearInterval(timer);
  },[isPlaying,field]);
  const position=field.trajectory[cursor].position_mm;
  return <div data-testid="cam-toolpath-viewer" data-segments={cursor}>
    <div className="my-2 h-64 rounded border border-[var(--line)] bg-[var(--surface)]" aria-label="加工路径三维预览">
      <Canvas frameloop="demand" camera={{position:[100,-100,100],up:[0,0,1]}}>
        <ambientLight intensity={1.3}/><directionalLight position={[10,-20,30]} intensity={1.5}/><OrbitControls makeDefault/>
        <Bounds fit clip margin={1.2}><mesh geometry={target}><meshStandardMaterial color="#adb8b1" transparent opacity={0.65} side={DoubleSide}/></mesh>
          <lineSegments geometry={paths.feed}><lineBasicMaterial color="#2169bd"/></lineSegments>
          <lineSegments geometry={paths.rapid}><lineBasicMaterial color="#bd6833"/></lineSegments>
          <mesh position={[position[0],position[1],position[2]+cuttingLength/2]} rotation={[Math.PI/2,0,0]}>
            <cylinderGeometry args={[field.cutter_diameter_mm/2,field.cutter_diameter_mm/2,cuttingLength,24]}/><meshStandardMaterial color="#d4aa48" transparent opacity={0.6}/>
          </mesh>
        </Bounds>
      </Canvas>
    </div>
    <p className="type-caption">蓝色：进给 · 橙色：快速移动 · 金色：刀具</p>
    <label className="my-2 block type-caption">路径段 {cursor} / {field.trajectory.length-1}<input className="w-full" aria-label="加工路径进度" type="range" min={0} max={field.trajectory.length-1} value={cursor} onChange={e=>{setPlaying(false);setCursor(Number(e.target.value));}}/></label>
    <button type="button" className="workspace-button" onClick={()=>{if (!isPlaying && cursor===field.trajectory.length-1) setCursor(0);setPlaying(!isPlaying);}}>{isPlaying ? '暂停路径播放' : '播放加工路径'}</button>
    <p className="my-2 type-caption">刀尖 G54 坐标：[{position.map(v=>v.toFixed(3)).join(', ')}] mm</p>
  </div>;
}
