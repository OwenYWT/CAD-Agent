import { useEffect, useEffectEvent, useRef, useState } from 'react';
import { authFetch } from '../../auth';
import { API_BASE, EngineeringApiError, readJson } from '../../services/clients/http';
import type { DurableTaskSubmittedEvent, ManufacturingProfile } from '../../types/nativeImport';

export default function NativeImportPanel({sessionId,panelId,profile,onImported}: {
  sessionId:string;panelId:string;profile:ManufacturingProfile;onImported:(receipt:DurableTaskSubmittedEvent)=>void;
}) {
  const storageKey=`cad-native-import:${sessionId}:${panelId}`;
  const [file,setFile]=useState<File|null>(null),[error,setError]=useState(''),[pending,setPending]=useState(false);
  const [receiptKey,setReceiptKey]=useState(()=>sessionStorage.getItem(storageKey));
  const [receiptRetry,setReceiptRetry]=useState(0);
  const busy=useRef(false);
  const onResult=useEffectEvent(onImported);
  useEffect(()=>{
    if (!receiptKey) return;
    const controller=new AbortController();
    void authFetch(`${API_BASE}/api/documents/imports/receipt?${new URLSearchParams({session_id:sessionId,panel_id:panelId,idempotency_key:receiptKey})}`,{signal:controller.signal})
      .then(async response=>{
        if (response.status===404) {sessionStorage.removeItem(storageKey);setReceiptKey(null);return;}
        const receipt=await readJson<DurableTaskSubmittedEvent>(response,'导入状态查询失败');
        if (!controller.signal.aborted) {sessionStorage.removeItem(storageKey);setReceiptKey(null);onResult(receipt);}
      }).catch((e:unknown)=>{if (!controller.signal.aborted) setError(e instanceof Error?e.message:'原请求状态未知，请查询原请求');});
    return ()=>controller.abort();
  },[receiptKey,receiptRetry,sessionId,panelId,storageKey]);
  const submit=async()=>{
    if (!file || busy.current || receiptKey) return;
    if (!/\.(fcstd|step|stp)$/i.test(file.name) || !file.size || file.size>64*1024*1024) {setError('请选择不超过 64 MiB 的 FCStd、STEP 或 STP 文件');return;}
    busy.current=true;setPending(true);setError('');
    const key=crypto.randomUUID();sessionStorage.setItem(storageKey,key);
    const body=new FormData();body.set('file',file);body.set('session_id',sessionId);body.set('panel_id',panelId);
    body.set('idempotency_key',key);body.set('manufacturing_profile',JSON.stringify(profile));
    try {
      const receipt=await readJson<DurableTaskSubmittedEvent>(await authFetch(`${API_BASE}/api/documents/imports`,{method:'POST',body}),'导入提交失败');
      sessionStorage.removeItem(storageKey);onImported(receipt);
    } catch(e) {
      if (e instanceof EngineeringApiError && e.httpStatus!==undefined && e.httpStatus<500) sessionStorage.removeItem(storageKey);
      else setReceiptKey(key);
      setError(e instanceof Error?e.message:'原导入是否受理尚未确认，正在查询原请求');
    } finally {busy.current=false;setPending(false);}
  };
  return <details className="mt-5 rounded-lg border border-[var(--line)] bg-white p-4"><summary className="cursor-pointer type-section-heading">打开已有 FCStd / STEP</summary>
    <p className="my-2 type-body">FCStd 保留支持的原生特征；STEP 导入实体边界，后续可添加原生特征。真实几何检查通过后生成候选，审核保存后进入统一编辑流程。</p>
    <label className="block type-control">工程文件<input type="file" accept=".fcstd,.FCStd,.step,.stp" aria-label="导入原生工程文件" disabled={pending || !!receiptKey} className="my-2 block w-full" onChange={e=>setFile(e.target.files?.[0] || null)}/></label>
    <button type="button" className="workspace-button" disabled={!file || pending || !!receiptKey} onClick={()=>void submit()}>{pending?'正在上传':'导入并检查'}</button>
    {receiptKey?<button type="button" className="workspace-button ml-2" onClick={()=>setReceiptRetry(value=>value+1)}>查询原导入请求</button>:null}
    {error?<p className="my-2 type-caption text-red-700" role="alert">{error}</p>:null}
  </details>;
}
