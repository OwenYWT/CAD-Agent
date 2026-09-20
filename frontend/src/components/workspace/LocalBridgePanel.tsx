import { useEffect,useState } from 'react';
import type { CloudDocument } from '../../types/document';
import type { BridgeState,BridgePairing } from '../../types/localBridge';
import type { DocumentRelease } from '../../types/release';
import { createLocalBridgePairing, deliverLocalRelease, downloadLocalBridgeClient, getLocalBridges, listDocumentReleases, revokeLocalBridge } from "../../services/clients/delivery";

const inputClass='mt-1 w-full rounded border border-[var(--line)] bg-white p-2';
const statuses:Record<string,string>={queued:'等待本地连接',leased:'正在交付',delivered:'文件已送达',failed:'交付失败',cancelled:'已取消'};

export default function LocalBridgePanel({document}:{document:CloudDocument}) {
  const [open,setOpen]=useState(false),[state,setState]=useState<BridgeState>({bridges:[],deliveries:[]});
  const [releases,setReleases]=useState<DocumentRelease[]>([]),[pair,setPair]=useState<BridgePairing | null>(null);
  const [label,setLabel]=useState(''),[bridgeId,setBridgeId]=useState(''),[releaseId,setReleaseId]=useState('');
  const [reload,setReload]=useState(0),[error,setError]=useState(''),[pending,setPending]=useState(false);
  useEffect(()=>{
    if (!open) return;
    const controller=new AbortController();let timer:ReturnType<typeof setTimeout>;
    const poll=async()=>{
      try {
        const [data,rows]=await Promise.all([getLocalBridges(document.document_id,controller.signal),listDocumentReleases(document.document_id,controller.signal)]);
        if (controller.signal.aborted) return;
        setState(data);setReleases(rows.filter(r=>r.status==='succeeded'));
        setPair(current=>current && data.bridges.some(b=>b.id===current.bridge_id && (b.paired_at || b.revoked_at)) ? null : current);
      } catch(e) {if (!controller.signal.aborted) setError(e instanceof Error ? e.message : '本地连接读取失败');}
      if (!controller.signal.aborted) timer=setTimeout(()=>void poll(),5000);
    };void poll();return ()=>{controller.abort();clearTimeout(timer);};
  },[document.document_id,open,reload]);
  const action=async(work:()=>Promise<unknown>)=>{
    setPending(true);setError('');try {await work();setReload(v=>v+1);} catch(e) {setError(e instanceof Error ? e.message : '本地连接操作失败');} finally {setPending(false);}
  };
  const available=state.bridges.filter(b=>b.paired_at && !b.revoked_at);
  return <section className="ww-inspector-section" data-testid="local-bridge-panel" aria-label="本地交付"><details open={open} onToggle={e=>setOpen(e.currentTarget.open)}>
    <summary className="cursor-pointer type-section-heading">本地 Bridge 交付</summary>
    <p className="my-2 type-caption">把已发布的 CAD、BOM 与工程证据交付到本地或企业共享目录。客户端逐项写入并校验后显示送达。</p>
    {document.can_share ? <details className="my-2"><summary className="cursor-pointer type-caption">配对本地客户端</summary>
      <p className="my-2 type-caption">需要 macOS 或 Linux 与 Python 3.11 以上版本。在下载文件所在目录运行以下命令，按提示填写实际输出目录和配对码。</p>
      <button className="workspace-button" type="button" onClick={()=>void action(downloadLocalBridgeClient)}>下载本地客户端</button>
      <pre className="my-2 whitespace-pre-wrap break-all type-caption">{`python3 cad_local_bridge.py pair --server '${window.location.origin}' --config "$HOME/.cad-bridge.json"`}</pre>
      <form className="space-y-2" onSubmit={e=>{e.preventDefault();void action(async()=>setPair(await createLocalBridgePairing(document.document_id,label.trim())));}}>
        <label className="block type-caption">连接名称<input className={inputClass} aria-label="本地连接名称" maxLength={120} value={label} onChange={e=>setLabel(e.target.value)}/></label>
        <button type="submit" className="workspace-button" disabled={pending || !label.trim()}>生成一次性配对码</button>
      </form>
      {pair ? <div className="my-2 type-caption"><p>将配对码输入本地终端，10 分钟内有效，仅可使用一次。</p>
        <input className={inputClass} aria-label="一次性配对码" readOnly value={pair.pairing_code}/>
        <button className="underline" type="button" onClick={()=>void action(()=>navigator.clipboard.writeText(pair.pairing_code))}>复制配对码</button></div> : null}
      <p className="my-2 type-caption">配对后持续运行：</p><pre className="whitespace-pre-wrap break-all type-caption">python3 cad_local_bridge.py run --config "$HOME/.cad-bridge.json"</pre>
    </details> : null}
    <div className="my-2 space-y-2" aria-label="本地连接列表">{!state.bridges.length ? <p className="type-caption">尚未配对本地连接</p> : state.bridges.map(b=><div className="rounded border border-[var(--line)] p-2 type-caption" key={b.id} data-bridge={b.id}>
      <p>{b.label} · {b.revoked_at ? '已撤销' : !b.paired_at ? '待配对' : b.online ? '在线' : '离线'}</p>
      {b.client_info.target_label ? <p>目标目录：{b.client_info.target_label} · {b.client_info.hostname}</p> : null}
      {document.can_share && !b.revoked_at ? <button type="button" className="underline" disabled={pending} onClick={()=>void action(()=>revokeLocalBridge(document.document_id,b.id))}>撤销连接</button> : null}
    </div>)}</div>
    {document.can_edit ? <form className="space-y-2" onSubmit={e=>{e.preventDefault();void action(()=>deliverLocalRelease(document.document_id,releaseId,bridgeId));}}>
      <label className="block type-caption">发布版本<select aria-label="本地交付发布版本" className={inputClass} value={releaseId} onChange={e=>setReleaseId(e.target.value)}>
        <option value="">选择已完成发布</option>{releases.map(r=><option value={r.release_id} key={r.release_id}>{r.release_name} · 版本 {r.source_state_version}</option>)}</select></label>
      <label className="block type-caption">目标连接<select aria-label="本地交付目标连接" className={inputClass} value={bridgeId} onChange={e=>setBridgeId(e.target.value)}>
        <option value="">选择本地连接</option>{available.map(b=><option key={b.id} value={b.id}>{b.label} · {b.online ? '在线' : '离线，将等待连接'}</option>)}</select></label>
      <button type="submit" className="workspace-button" disabled={pending || !releases.some(r=>r.release_id===releaseId) || !available.some(b=>b.id===bridgeId)}>交付发布文件</button>
    </form> : null}
    {error ? <p role="alert" className="my-2 type-caption text-red-700">{error}</p> : null}
    <div className="mt-3 space-y-2" aria-label="本地交付记录">{state.deliveries.map(d=><div className="rounded border border-[var(--line)] p-2 type-caption" data-delivery={d.id} key={d.id}>
      <p>{releases.find(r=>r.release_id===d.release_id)?.release_name || d.release_id.slice(0,8)} · {statuses[d.status] || d.status}</p>
      {d.receipt ? <p className="break-all">已校验 {d.receipt.file_count} 个文件：{d.receipt.directory}</p> : null}
      {d.error_message ? <p className="text-red-700" role="alert">{d.error_message}</p> : null}
    </div>)}</div>
  </details></section>;
}
