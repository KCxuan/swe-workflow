import React, { useEffect, useState } from 'react';
import { createRoot } from 'react-dom/client';
import './style.css';

type Criterion = {id:string; description:string; kind:string};
type Plan = {title:string; summary:string; features:string[]; pages:string[]; tech_stack:string; acceptance:Criterion[]};
type Run = {id:string; path:string; mode:'new'|'modify'; request:string; status:string; phase:string; state:any; preview_url:string|null; report_path:string|null; screenshots:string[]};
type Event = {id:number; type:string; payload:any; created:string};
const terminal = new Set(['completed','failed','stopped','ended']);
const names:Record<string,string> = {planner:'需求 Agent',coder:'编码 Agent',reviewer:'审查 Agent',planning:'整理需求',coding:'编写代码',preparing:'准备运行',reviewing:'实际检查',evaluating:'核对结果',begin_repair:'准备修复',repairing:'修复中',confirmation:'需求确认',prepare:'准备运行',evaluate:'核对结果',running:'执行中',waiting_confirmation:'等待确认',completed:'验收通过',failed:'未完成',stopped:'已停止',stopping:'停止中',ended:'已结束'};
async function api<T=any>(path:string, body?:unknown, method?:string):Promise<T> {
  const r=await fetch('/api'+path,{method:method||(body===undefined?'GET':'POST'),headers:body===undefined?{}:{'Content-Type':'application/json'},body:body===undefined?undefined:JSON.stringify(body)});
  const value=await r.json();
  if(!r.ok)throw Object.assign(new Error(typeof value.detail==='string'?value.detail:JSON.stringify(value.detail)),{status:r.status});
  return value;
}
function PlanEditor({plan,busy,decide}:{plan:Plan;busy:boolean;decide:(kind:string,plan?:Plan)=>void}) {
  const [draft,setDraft]=useState(()=>structuredClone(plan));
  function field<K extends keyof Plan>(key:K,value:Plan[K]){setDraft({...draft,[key]:value});}
  return <div className="plan-editor"><div className="section-label">确认本次开发方案</div>
    <label>项目名称<input value={draft.title} onChange={e=>field('title',e.target.value)}/></label>
    <label>目标说明<textarea rows={3} value={draft.summary} onChange={e=>field('summary',e.target.value)}/></label>
    <label>功能清单 · 每行一项<textarea rows={3} value={draft.features.join('\n')} onChange={e=>field('features',e.target.value.split('\n'))}/></label>
    <label>页面结构 · 每行一项<textarea rows={2} value={draft.pages.join('\n')} onChange={e=>field('pages',e.target.value.split('\n'))}/></label>
    <label>技术方案<textarea rows={2} value={draft.tech_stack} onChange={e=>field('tech_stack',e.target.value)}/></label>
    <div className="section-label">验收清单<button className="text" onClick={()=>field('acceptance',[...draft.acceptance,{id:'AC-'+crypto.randomUUID().slice(0,6),description:'',kind:'browser'}])}>＋ 添加</button></div>
    {draft.acceptance.map((c,i)=><div className="criterion-edit" key={c.id}><small>{c.id}</small><select aria-label={'检查类型 '+c.id} value={c.kind} onChange={e=>field('acceptance',draft.acceptance.map((v,j)=>i===j?{...v,kind:e.target.value}:v))}><option value="browser">页面交互</option><option value="api">接口</option><option value="data">数据读写</option><option value="code">代码检查</option></select><textarea aria-label={'验收项 '+c.id} rows={2} value={c.description} onChange={e=>field('acceptance',draft.acceptance.map((v,j)=>i===j?{...v,description:e.target.value}:v))}/><button className="text" aria-label={'删除 '+c.id} onClick={()=>field('acceptance',draft.acceptance.filter((_,j)=>i!==j))}>删除</button></div>)}
    <button className="primary full" disabled={busy} onClick={()=>decide('confirm',draft)}>确认方案，开始开发 ↗</button>
    <button className="text full" disabled={busy} onClick={()=>decide('end')}>结束本次任务</button>
  </div>;
}
function eventTitle(e:Event){
  if(e.type==='agent.started')return names[e.payload.role]+' 开始';
  if(e.type==='agent.finished')return names[e.payload.role]+' 完成';
  if(e.type==='agent.retrying')return e.payload.message;
  if(e.type==='agent.output_invalid')return '正在补全角色结果';
  if(e.type==='file.written')return '写入 '+e.payload.path;
  if(e.type==='service.ready')return e.payload.name+' 已启动';
  if(e.type==='tool.result')return e.payload.tool+' · '+(e.payload.passed?'已执行':'检查失败');
  if(e.type==='repair.started')return '第 '+e.payload.round+' 轮修复';
  if(e.type==='run.waiting')return '方案已整理，等待你确认';
  if(e.type==='run.failed')return e.payload.error;
  if(e.type==='run.finished')return names[e.payload.status];
  if(e.type==='review.results')return '逐项检查已完成';
  if(e.type==='node.finished')return (names[e.payload.node]||e.payload.node)+' 节点完成';
  return e.type==='setup.started'?'准备依赖与构建':'开始本次任务';
}
function App(){
  const [run,setRun]=useState<Run|null>(null),[events,setEvents]=useState<Event[]>([]),[health,setHealth]=useState<any>({});
  const [path,setPath]=useState(''),[mode,setMode]=useState<'new'|'modify'>('new'),[request,setRequest]=useState('');
  const [busy,setBusy]=useState(false),[error,setError]=useState(''),[tab,setTab]=useState('preview'),[frameKey,setFrameKey]=useState(0);
  async function action(fn:()=>Promise<void>){setBusy(true);setError('');try{await fn();}catch(e){setError((e as Error).message);}finally{setBusy(false);}}
  async function refresh(id:string){try{const r=await api<Run>('/runs/'+id);setRun(old=>old?.id===id?r:old);}catch(e){if((e as any).status===404){const current=await api<Run|null>('/current').catch(()=>null);setRun(old=>old?.id===id?current:old);if(current){setPath(current.path);setMode(current.mode);}else setError('服务会话已结束。源码和报告保留在原目录，可重新提交任务。');}}}
  useEffect(()=>{api<Run|null>('/current').then(r=>{setRun(r);if(r){setPath(r.path);setMode(r.mode);}}).catch(()=>{});},[]);
  useEffect(()=>{const poll=()=>api('/health').then(setHealth).catch(()=>setHealth({ok:false}));poll();const t=setInterval(poll,3000);return()=>clearInterval(t);},[]);
  useEffect(()=>{
    setEvents([]);if(!run)return;
    const id=run.id,source=new EventSource('/api/runs/'+id+'/events');
    source.onmessage=e=>{const item=JSON.parse(e.data);setEvents(old=>old.some(v=>v.id===item.id)?old:[...old,item].slice(-300));};
    source.addEventListener('closed',()=>{source.close();refresh(id);});
    return()=>source.close();
  },[run?.id]);
  useEffect(()=>{if(!run)return;const t=setInterval(()=>refresh(run.id),1200);return()=>clearInterval(t);},[run?.id]);
  const active=!!run&&['running','stopping'].includes(run.status),finished=!!run&&terminal.has(run.status);
  const report=run?.state.review_report,passed=report?.results?.filter((r:any)=>r.status==='passed').length||0,total=run?.state.plan?.acceptance?.length||0;
  async function decide(kind:string,plan?:Plan){if(run)await action(async()=>setRun(await api('/runs/'+run.id+'/decision',{kind,plan})));}
  function newTask(){if(run){setPath(run.path);setMode('modify');}setRun(null);setRequest('');setTab('preview');setError('');}
  return <main className="shell"><header><a className="brand" href="/"><span>工</span><div>工序<small>SOFTWARE DEVELOPMENT WORKFLOW</small></div></a><div className="connection"><i className={health.ok?'online':''}/>{health.ok?'本地服务已连接':'连接本地服务中'}<small>{health.model||'等待模型配置'}</small></div></header>
    <section className="page-title"><div><div className="eyebrow">需求 → 编码 → 检查</div><h1>一次需求，一条开发流程。</h1><p>三个 Agent 协作，源码和结果直接留在你的文件夹。</p></div><div className="task-actions">{finished&&<button className="secondary" onClick={newTask}>新任务 / 继续修改 ↗</button>}<span className={'status '+(run?.status||'idle')}><i/>{run?names[run.status]:'等待开始'}</span></div></section>
    {error&&<div className="error" role="alert">{error}<button className="text" onClick={()=>setError('')}>关闭</button></div>}
    <div className="workbench"><section className="demand panel"><div className="panel-heading"><b>01 / 需求与方案</b><small>确认后才写入源码</small></div><div className="panel-body">
      {!run?<form onSubmit={e=>{e.preventDefault();action(async()=>{setRun(await api('/runs',{path,mode,request}));setTab('preview');});}}><label>本次开发方式<select value={mode} onChange={e=>setMode(e.target.value as 'new'|'modify')}><option value="new">新建项目 · 使用空文件夹</option><option value="modify">继续修改 · 使用已有源码</option></select></label><label>开发目录<div className="path-row"><input aria-label="开发目录" required placeholder="D:\Projects\my-app" value={path} onChange={e=>setPath(e.target.value)}/><button type="button" className="secondary" disabled={busy} onClick={()=>action(async()=>{const r=await api('/pick-directory',{});if(r.path)setPath(r.path);})}>选择</button></div></label><label>你想开发什么？<textarea required rows={7} placeholder="例如：做一个计数器，支持加一和重置，使用原生 HTML/CSS/JS。" value={request} onChange={e=>setRequest(e.target.value)}/></label><button className="primary full" disabled={busy||!path.trim()||!request.trim()}>整理需求 ↗</button><p className="hint">支持单页、多页面及带后端和数据库的应用。选择继续修改时，会在现有源码上工作。</p></form>:<><div className="user-message"><span>你的需求</span><p>{run.request}</p></div><div className="workspace-path"><span>{run.mode==='new'?'新建项目':'继续修改'}</span><code>{run.path}</code><button className="text" onClick={()=>action(async()=>{await navigator.clipboard.writeText(run.path);})}>复制目录路径</button></div>
        {run.status==='waiting_confirmation'?<PlanEditor key={run.id} plan={run.state.plan} busy={busy} decide={decide}/>:run.state.plan&&<div className="plan"><div className="section-label">本次方案</div><h2>{run.state.plan.title}</h2><p>{run.state.plan.summary}</p><ul>{run.state.plan.features.map((f:string,i:number)=><li key={i}>{f}</li>)}</ul><div className="stack">{run.state.plan.tech_stack}</div><small>页面：{run.state.plan.pages.join(' / ')}</small></div>}
        {run.state.error&&<div className="inline-error">{run.state.error}</div>}{active&&<button className="stop full" disabled={busy||run.status==='stopping'} onClick={()=>action(async()=>setRun(await api('/runs/'+run.id+'/stop',{})))}>■ {run.status==='stopping'?'停止中…':'停止当前任务'}</button>}
        {run.report_path&&<div className="saved-note">源码和最终报告已留在本地<code>{run.report_path}</code></div>}
        {finished&&<p className="hint">下一次修改可选择同一目录，重新提交需求并确认。</p>}
      </>}
    </div></section>
    <section className="progress panel"><div className="panel-heading"><b>02 / 协作进度</b><small>本次任务</small></div><div className="stages">{['planner','coder','reviewer'].map((role,i)=><div key={role} className={'stage '+(active&&[role,{planner:'planning',coder:'coding',reviewer:'reviewing'}[role]].includes(run?.phase||'')?'current':'')}><span>0{i+1}</span><div>{names[role]}<small>{['整理方案','写入并启动','实际操作验收'][i]}</small></div></div>)}</div><div className="repairs"><span>已完成修复</span><b>{run?.state.repair_count||0}<small> / 3</small></b></div><div className="events">{events.length?events.map(e=><details key={e.id}><summary><time>{new Date(e.created).toLocaleTimeString('zh-CN',{hour12:false})}</time>{eventTitle(e)}</summary><p>{e.payload.detail||e.payload.summary||e.payload.message||e.payload.error||eventTitle(e)}</p></details>):<p className="hint">开始后，这里会显示角色交接、文件写入和检查进度。</p>}</div></section>
    <section className="output panel"><div className="tabs"><div><button className={tab==='preview'?'selected':''} onClick={()=>setTab('preview')}>应用预览</button><button className={tab==='report'?'selected':''} onClick={()=>setTab('report')}>检查报告{total>0&&<small>{passed}/{total}</small>}</button></div><span>03 / RESULT</span></div>
      {tab==='preview'?<><div className="preview-bar"><code>{run?.preview_url||'应用尚未启动'}</code><div><button className="text" disabled={busy||active||!run?.state.coding_result} onClick={()=>run&&action(async()=>{setRun(await api('/runs/'+run.id+'/preview',{}));setFrameKey(k=>k+1);})}>启动预览</button><button className="text" disabled={!run?.preview_url} onClick={()=>setFrameKey(k=>k+1)}>刷新</button>{run?.preview_url&&<a href={run.preview_url} target="_blank" rel="noreferrer">独立打开 ↗</a>}<button className="text" disabled={busy||active||!run?.preview_url} onClick={()=>run&&action(async()=>setRun(await api('/runs/'+run.id+'/preview',undefined,'DELETE')))}>停止预览</button></div></div>{run?.preview_url?<iframe title="生成应用预览" key={frameKey+run.preview_url} src={run.preview_url} sandbox="allow-scripts allow-forms allow-same-origin allow-modals allow-downloads"/>:<div className="empty"><div className="window-outline"><i/><i/><i/><span>YOUR NEXT APP</span></div><h2>让需求成为可运行的软件</h2><p>确认方案后，Agent 将写入源码、启动应用并逐项检查。</p></div>}</>:<div className="report"><div className="report-head"><div className="eyebrow">VERIFICATION REPORT</div><h2>{passed}<small> / {total} 项通过</small></h2><p>{report?.summary||'实际检查后，这里会展示每一项验收结果。'}</p>{run?.report_path&&<a className="download" href={'/api/runs/'+run.id+'/report'} download>下载本次报告 ↗</a>}</div>{report?.results?.map((r:any)=><article className={r.status} key={r.criterion_id}><span>{r.status==='passed'?'✓':r.status==='failed'?'×':'—'}</span><div><h3>{run?.state.plan.acceptance.find((c:Criterion)=>c.id===r.criterion_id)?.description||r.criterion_id}<small>{r.status==='passed'?'通过':r.status==='failed'?'失败':'未执行'}</small></h3><p>{r.detail}</p><small>{r.evidence_ids.length} 条实际工具证据已记录</small></div></article>)}{run&&run.state.issues?.length>0&&<div className="inline-error"><b>待解决问题</b><ul>{run.state.issues.map((s:string,i:number)=><li key={i}>{s}</li>)}</ul></div>}<div className="screenshots">{run?.screenshots.map(name=><a key={name} href={'/api/runs/'+run.id+'/screenshots/'+name} target="_blank" rel="noreferrer"><img alt="实际浏览器检查截图" src={'/api/runs/'+run.id+'/screenshots/'+name}/></a>)}</div></div>}
    </section></div><footer><span>本轮状态保存在当前会话，最终报告保存在项目的 .workflow 文件夹。</span><span>LANGGRAPH / LANGCHAIN / PLAYWRIGHT</span></footer>
  </main>;
}
createRoot(document.getElementById('root')!).render(<React.StrictMode><App/></React.StrictMode>);
