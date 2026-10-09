import { useCallback, useMemo, useState } from 'react';
import { Box3, Vector3 } from 'three';
import DocumentSceneViewer from './DocumentSceneViewer';
import type { CameraPose } from './CameraLink';
type Bounds={min:number[];max:number[]};
export default function RevisionComparison({documentId,leftRevision,rightRevision}: {documentId:string;leftRevision:string;rightRevision:string}) {
  const [pose,setPose]=useState<CameraPose|null>(null),[ranges,setRanges]=useState<Record<string,Bounds>>({});
  const onPose=useCallback((next:CameraPose)=>setPose(old=>old && JSON.stringify({...old,source:''})===JSON.stringify({...next,source:''})?old:next),[]);
  const onBounds=useCallback((revision:string,bounds:Bounds)=>setRanges(old=>JSON.stringify(old[revision])===JSON.stringify(bounds)?old:{...old,[revision]:bounds}),[]);
  const combined=useMemo(()=>{
    if (!ranges[leftRevision] || !ranges[rightRevision]) return null;
    return new Box3().union(new Box3(new Vector3(...ranges[leftRevision].min),new Vector3(...ranges[leftRevision].max)))
      .union(new Box3(new Vector3(...ranges[rightRevision].min),new Vector3(...ranges[rightRevision].max)));
  },[ranges,leftRevision,rightRevision]);
  return <section className="p-3" aria-label="修订图形对比"><p className="type-caption mb-2">左右使用同一世界坐标与联动相机。图形显示采用网格；精确变化以原生参数和测量证据为准。两侧均只读。</p>
    <div className="grid gap-3 md:grid-cols-2">{[leftRevision,rightRevision].map((revision,index)=><section className="min-w-0" key={`${index}:${revision}`}><h3 className="type-control break-all"><span>{index===0?'保存基线':'查看修订'}</span> · <span data-i18n-skip>{revision}</span></h3><div className="h-[min(55vh,500px)] min-h-64 border border-[var(--line)]">
      <DocumentSceneViewer documentId={documentId} revisionId={revision} fitRequest={combined?1:0} comparison={{id:String(index),pose,onPose,onBounds,bounds:combined}}/>
    </div></section>)}</div>
  </section>;
}
