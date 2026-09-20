'use strict';
const $ = id => document.getElementById(id);
let token = sessionStorage.getItem('cad-monitor-token'), account = '', mode = 'tasks', offset = 0, busy = false, detailsRequest = 0;
const labels = {pending:'等待执行',planning:'规划中',running:'执行中',waiting_confirmation:'待确认',cancelling:'取消中',succeeded:'成功',failed:'失败',cancelled:'已取消',timed_out:'超时',started:'未结束',truncated:'输出截断'};
const number = value => value == null ? '未知' : Number(value).toLocaleString('zh-CN');
const date = value => value ? new Date(value).toLocaleString('zh-CN') : '—';
const duration = value => value == null ? '未知' : `${(Number(value)/1000).toFixed(1)} s`;
function showError(error) { $('error').hidden=false; $('error').textContent=error.message || String(error); }
async function api(path, options={}) {
  const response = await fetch(path,{...options,headers:{...options.headers,...(token?{Authorization:`Bearer ${token}`}:{})}});
  const body = await response.json();
  if (!response.ok) { if(response.status===401) logout(); throw Error(typeof body.detail==='string'?body.detail:`请求失败 (${response.status})`); }
  return body;
}
function logout() { detailsRequest++; token=null;sessionStorage.removeItem('cad-monitor-token');$('dashboard').hidden=true;$('login').hidden=false;$('logout').hidden=true;$('accounts').replaceChildren();$('details').replaceChildren(); }
function range() {
  const start=new Date(`${$('start').value}T00:00:00`), end=new Date(`${$('end').value}T00:00:00`);end.setDate(end.getDate()+1);
  if(!Number.isFinite(start.getTime())||!Number.isFinite(end.getTime())||start>=end) throw Error('请选择有效的日期范围');
  return new URLSearchParams({start:start.toISOString(),end:end.toISOString()});
}
function row(parent, values) { const tr=document.createElement('tr');for(const value of values){const td=document.createElement('td');if(value instanceof Node)td.append(value);else td.textContent=String(value);tr.append(td);}parent.append(tr); }
function status(value) { const span=document.createElement('span');span.textContent=labels[value]||value;span.className=['failed','timed_out','truncated'].includes(value)?'bad':value==='succeeded'?'good':'';return span; }
function details(value) { const el=document.createElement('details'), title=document.createElement('summary'), text=document.createElement('code');title.textContent='查看详情';text.textContent=JSON.stringify(value,null,2);el.append(title,text);return el; }
async function loadDetails() {
  const request = ++detailsRequest;
  const query=range();if(account)query.set('account',account);query.set('offset',offset);query.set('limit',30);
  const data=await api(`api/monitor/${mode}?${query}`);if(request!==detailsRequest)return;$('details').replaceChildren();$('detail-head').replaceChildren();
  const heads=mode==='tasks'?['账号','任务类型','状态','创建时间','结束时间','错误码','详情']:['账号','模型 / 服务','阶段','结果','输入 / 输出 Token','总 Token','耗时','开始时间','详情'];
  const tr=document.createElement('tr');for(const h of heads){const th=document.createElement('th');th.textContent=h;tr.append(th);}$('detail-head').append(tr);
  for(const item of data.items)row($('details'),mode==='tasks'?[item.name||item.account,item.kind,status(item.status),date(item.created_at),date(item.completed_at),item.error_code||'—',details(item)]:[item.name||item.account,`${item.model} / ${item.provider}`,item.activity_type||'直接调用',status(item.status),`${number(item.prompt_tokens)} / ${number(item.completion_tokens)}`,number(item.total_tokens),duration(item.duration_ms),date(item.started_at),details(item)]);
  if(!data.items.length)row($('details'),['此范围内没有记录']);
  $('prev').disabled=offset===0;$('next').disabled=data.items.length<30||offset+data.items.length>=Number(data.items[0]?.total_rows||0);$('page').textContent=`第 ${offset/30+1} 页`;
}
async function refresh() {
  if(busy)return;busy=true;$('refresh').disabled=true;$('error').hidden=true;
  try {
    const data=await api(`api/monitor/accounts?${range()}`);
    $('login').hidden=true;$('dashboard').hidden=false;$('logout').hidden=false;$('accounts').replaceChildren();$('stats').replaceChildren();
    const all=data.accounts, sum=key=>all.reduce((a,b)=>a+Number(b[key]||0),0);
    for(const [label,value] of [['有活动的账号',all.filter(x=>x.tasks||x.calls).length],['任务总数',sum('tasks')],['模型调用次数',sum('calls')],['已知总 Token',all.some(x=>x.total_tokens!=null)?sum('total_tokens'):null]]){const card=document.createElement('div'),strong=document.createElement('strong');card.textContent=label;strong.textContent=number(value);card.append(strong);$('stats').append(card);}
    for(const item of all){const b=document.createElement('button');b.textContent=item.name||item.account;b.onclick=()=>{account=item.account;offset=0;$('details-title').textContent=`${b.textContent} · 使用明细`;$('clear-account').hidden=false;loadDetails().catch(showError);};row($('accounts'),[b,number(item.tasks),`${number(item.succeeded)} / ${number(item.failed)}`,`${number(item.active)} / ${number(item.cancelled)}`,number(item.calls),`${number(item.call_failures)} / ${number(item.truncated)}`,`${number(item.prompt_tokens)} / ${number(item.completion_tokens)}`,number(item.total_tokens),number(item.unknown_usage_calls),duration(item.avg_duration_ms),date(item.last_used_at)]);}
    if(!all.length)row($('accounts'),['尚无账号记录']);
    $('coverage').textContent=data.first_recorded_call?`模型调用记录起始：${date(data.first_recorded_call)}。此前任务可查询，但历史调用用量未补造。`:'尚无模型调用记录。任务历史可查询，历史 Token 用量未知。';
    await loadDetails();$('updated').textContent=`更新于 ${date(new Date())}`;
  } catch(error){showError(error);} finally{busy=false;$('refresh').disabled=false;}
}
$('login-form').onsubmit=async event=>{event.preventDefault();const button=event.submitter;button.disabled=true;$('error').hidden=true;try{const data=await api('api/auth/login/password',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(Object.fromEntries(new FormData(event.target)))});if(!data.user.is_admin)throw Error('仅平台管理员可访问监控面板');token=data.token;sessionStorage.setItem('cad-monitor-token',token);event.target.reset();await refresh();}catch(error){showError(error);}finally{button.disabled=false;}};
$('logout').onclick=logout;$('refresh').onclick=()=>{offset=0;refresh();};
$('clear-account').onclick=()=>{account='';offset=0;$('details-title').textContent='全部账号明细';$('clear-account').hidden=true;loadDetails().catch(showError);};
for(const tab of ['tasks','calls'])$(`${tab}-tab`).onclick=()=>{mode=tab;offset=0;for(const t of ['tasks','calls'])$(`${t}-tab`).setAttribute('aria-pressed',String(t===tab));loadDetails().catch(showError);};
$('prev').onclick=()=>{offset=Math.max(0,offset-30);loadDetails().catch(showError);};$('next').onclick=()=>{offset+=30;loadDetails().catch(showError);};
function localDay(d){return `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`;}
const today=new Date(), start=new Date();start.setDate(start.getDate()-6);$('start').value=localDay(start);$('end').value=localDay(today);if(token)refresh();
