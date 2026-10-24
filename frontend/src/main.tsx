import React, { useEffect, useState } from 'react';
import { createRoot } from 'react-dom/client';
import './style.css';

type Criterion = {id:string; description:string; kind:string};
type Plan = {title:string; summary:string; features:string[]; pages:string[]; tech_stack:string; acceptance:Criterion[]};
type Run = {id:string; project_id:string; request:string; status:string; phase:string; created:string; state:any; report_current?:boolean; preview_url?:string|null; parent_id?:string};
type Project = {id:string; name:string; path:string; latest_run?:string; created:string; runs?:Run[]};
type Event = {id:number; type:string; payload:any; created:string};
const labels:Record<string,string> = {running:'执行中',planning:'整理需求',planner:'需求 Agent',coder:'编码 Agent',coding:'编写代码',preparing:'启动应用',reviewer:'审查 Agent',reviewing:'实际检查',evaluating:'核对结果',repairing:'修复中',waiting_confirmation:'等待需求确认',waiting_repair:'等待修复决定',completed:'验收通过',failed:'执行失败',stopped:'已停止',stopping:'停止中',interrupted:'执行已中断',ended:'已结束'};
const terminal = new Set(['completed','failed','stopped','interrupted','ended']);
const phaseFor:Record<string,string>={planner:'planning',coder:'coding',reviewer:'reviewing'};

async function api<T=any>(path:string, body?:unknown, method?:string):Promise<T> {
  const response = await fetch('/api'+path,{method:method || (body===undefined?'GET':'POST'),headers:body===undefined?{}:{'Content-Type':'application/json'},body:body===undefined?undefined:JSON.stringify(body)});
  const value = await response.json();
  if(!response.ok) throw new Error(typeof value.detail==='string'?value.detail:JSON.stringify(value.detail));
  return value;
}
const clock = (t:string) => new Date(t).toLocaleTimeString('zh-CN',{hour12:false});

function PlanEditor({run,busy,onDecision}:{run:Run;busy:boolean;onDecision:(kind:string,plan?:Plan,feedback?:string)=>void}) {
  const [plan,setPlan] = useState<Plan>(run.state.plan);
  const [feedback,setFeedback] = useState('');
  useEffect(()=>setPlan(structuredClone(run.state.plan)),[run.id,run.state.plan_revision]);
  function field<K extends keyof Plan>(key:K,value:Plan[K]){setPlan({...plan,[key]:value});}
  return <div className="plan-editor">
    <div className="section-label">待确认提案 <span>REV {run.state.plan_revision}</span></div>
    <label>项目名称<input value={plan.title} onChange={e=>field('title',e.target.value)}/></label>
    <label>目标说明<textarea rows={2} value={plan.summary} onChange={e=>field('summary',e.target.value)}/></label>
    <label>功能清单 · 每行一项<textarea rows={4} value={plan.features.join('\n')} onChange={e=>field('features',e.target.value.split('\n'))}/></label>
    <label>页面结构 · 每行一项<textarea rows={2} value={plan.pages.join('\n')} onChange={e=>field('pages',e.target.value.split('\n'))}/></label>
    <label>技术方案<textarea rows={2} value={plan.tech_stack} onChange={e=>field('tech_stack',e.target.value)}/></label>
    <div className="section-label">验收清单 <button className="text-button" onClick={()=>field('acceptance',[...plan.acceptance,{id:'AC-'+crypto.randomUUID().slice(0,6),description:'',kind:'browser'}])}>＋ 添加</button></div>
    {plan.acceptance.map((c,i)=><div className="criterion-edit" key={c.id}><span>{c.id}</span><select value={c.kind} onChange={e=>field('acceptance',plan.acceptance.map((v,j)=>i===j?{...v,kind:e.target.value}:v))}><option value="browser">页面交互</option><option value="api">接口</option><option value="data">数据保存</option><option value="code">代码</option></select><textarea rows={2} value={c.description} onChange={e=>field('acceptance',plan.acceptance.map((v,j)=>i===j?{...v,description:e.target.value}:v))}/><button className="text-button" aria-label="删除验收项" onClick={()=>field('acceptance',plan.acceptance.filter((_,j)=>i!==j))}>删除</button></div>)}
    <button className="primary full" disabled={busy} onClick={()=>onDecision('confirm',plan)}>确认需求，开始开发 <span>↗</span></button>
    <label>需要 Agent 重新整理？<textarea rows={2} placeholder="补充你的要求…" value={feedback} onChange={e=>setFeedback(e.target.value)}/></label>
    <button className="secondary full" disabled={busy||!feedback.trim()} onClick={()=>onDecision('revise',undefined,feedback)}>根据补充重新生成提案</button>
  </div>;
}

function Evidence({runId,ids}:{runId:string;ids:string[]}) {
  const [items,setItems]=useState<any[]>([]),[error,setError]=useState('');
  useEffect(()=>{api<any[]>('/runs/'+runId+'/evidence').then(setItems).catch(e=>setError(e.message));},[runId,ids.join(',')]);
  return <>{error&&<p>{error}</p>}{items.filter(e=>ids.includes(e.id)).map(e=><div className="evidence-item" key={e.id}><pre>{JSON.stringify(e.details,null,2)}</pre>{e.details.screenshot&&<a href={'/api/runs/'+runId+'/artifacts/'+e.details.screenshot} target="_blank" rel="noreferrer"><img alt="实际检查截图" src={'/api/runs/'+runId+'/artifacts/'+e.details.screenshot}/></a>}</div>)}</>;
}

function App(){
  const [projects,setProjects]=useState<Project[]>([]),[project,setProject]=useState<Project|null>(null),[run,setRun]=useState<Run|null>(null);
  const [health,setHealth]=useState<any>({}),[events,setEvents]=useState<Event[]>([]),[error,setError]=useState(''),[busy,setBusy]=useState(false);
  const [create,setCreate]=useState(false),[path,setPath]=useState(''),[name,setName]=useState(''),[request,setRequest]=useState('');
  const [tab,setTab]=useState('preview'),[fileList,setFileList]=useState<string[]>([]),[selectedFile,setSelectedFile]=useState(''),[code,setCode]=useState('');
  const [preview,setPreview]=useState(''),[frameKey,setFrameKey]=useState(0),[repairFeedback,setRepairFeedback]=useState('');
  const [lastProject]=useState(()=>localStorage.getItem('gongxu.project'));
  async function loadProjects(){const list=await api<Project[]>('/projects');setProjects(list);return list;}
  async function openProject(id:string){const p=await api<Project>('/projects/'+id);setProject(p);localStorage.setItem('gongxu.project',id);setRun(p.runs?.[0]||null);setPreview('');setSelectedFile('');setCode('');setTab('preview');setCreate(false);}
  async function refreshRun(id:string){try{const r=await api<Run>('/runs/'+id);setRun(old=>old?.id===r.id?r:old);}catch{setHealth({ok:false});}}
  async function action(fn:()=>Promise<void>){setBusy(true);setError('');try{await fn();}catch(e){setError((e as Error).message);}finally{setBusy(false);}}
  useEffect(()=>{loadProjects().then(list=>{if(lastProject&&list.some(p=>p.id===lastProject))openProject(lastProject);}).catch(e=>setError(e.message));},[]);
  useEffect(()=>{const poll=()=>api('/health').then(setHealth).catch(()=>setHealth({ok:false}));poll();const t=setInterval(poll,3000);return()=>clearInterval(t);},[]);
  useEffect(()=>{
    if(!run?.id){setEvents([]);return;}
    setEvents([]);const id=run.id;let timer:ReturnType<typeof setTimeout>|undefined;
    const source=new EventSource('/api/runs/'+id+'/events');
    source.onmessage=e=>{const event=JSON.parse(e.data);setEvents(old=>old.some(v=>v.id===event.id)?old:[...old,event].slice(-400));if(!timer)timer=setTimeout(()=>{refreshRun(id);timer=undefined;},250);};
    source.addEventListener('closed',()=>{source.close();refreshRun(id);loadProjects();});
    return()=>{source.close();if(timer)clearTimeout(timer);};
  },[run?.id]);
  useEffect(()=>{if(!run)return;const t=setInterval(()=>refreshRun(run.id),terminal.has(run.status)?5000:1500);return()=>clearInterval(t);},[run?.id,run?.status]);
  useEffect(()=>{setPreview(run?.preview_url||'');},[run?.id,run?.preview_url]);
  useEffect(()=>{if(project)api<string[]>('/projects/'+project.id+'/files').then(setFileList).catch(()=>setFileList([]));},[project?.id,run?.status,tab]);
  useEffect(()=>{if(project&&selectedFile)api('/projects/'+project.id+'/files/content?path='+encodeURIComponent(selectedFile)).then(v=>setCode(v.content)).catch(e=>setCode(e.message));},[project?.id,selectedFile,run?.status]);
  useEffect(()=>{if(project&&run&&terminal.has(run.status))api<Project>('/projects/'+project.id).then(p=>setProject(old=>old?.id===p.id?p:old)).catch(()=>{});},[project?.id,run?.id,run?.status]);
  const active=run&&['running','stopping'].includes(run.status);
  const report=run?.state.review_report;
  const passed=run?.report_current?report?.results?.filter((r:any)=>r.status==='passed').length||0:0;
  const expected=run?.state.plan?.acceptance?.length||0;
  async function decide(kind:string,plan?:Plan,feedback?:string){if(!run)return;await action(async()=>{const result=await api<Run>('/runs/'+run.id+'/decision',{kind,expected_revision:run.state.plan_revision,decision_id:crypto.randomUUID(),plan,feedback});setRun(result);});}
  function eventTitle(e:Event){if(e.type==='agent.started')return (labels[e.payload.role]||e.payload.role)+' 开始';if(e.type==='agent.retrying')return e.payload.message;if(e.type==='agent.output_invalid')return (labels[e.payload.role]||e.payload.role)+' 结果待补全';if(e.type==='agent.finished')return e.payload.summary;if(e.type==='file.written')return '写入 '+e.payload.path;if(e.type==='service.ready')return e.payload.name+' 服务就绪';if(e.type==='tool.result')return e.payload.tool+' · '+(e.payload.passed?'已执行':'检查失败');if(e.type==='repair.started')return `修复第 ${e.payload.batch} 批 · 第 ${e.payload.round} 轮`;if(e.type==='run.waiting')return e.payload.kind==='confirmation'?'需求提案等待确认':'本批修复结束，等待决定';if(e.type==='run.failed')return e.payload.error;if(e.type==='run.finished')return labels[e.payload.status]||e.payload.status;if(e.type==='node.finished')return (labels[e.payload.node]||e.payload.node)+' 节点完成';return e.type;}
  return <div className="app-shell">
    <aside className="sidebar"><a className="brand" href="/" aria-label="工序首页"><span className="brand-mark">工</span><div>工序<small>AGENT WORKBENCH</small></div></a>
      <div className="sidebar-caption">本地项目 <span>{String(projects.length).padStart(2,'0')}</span></div>
      <button className="new-project" onClick={()=>setCreate(true)}>＋ 新建项目</button>
      <nav>{projects.map(p=><button key={p.id} className={'project-link '+(project?.id===p.id?'selected':'')} onClick={()=>action(()=>openProject(p.id))}><span className="project-square">◇</span><span>{p.name}<small>{new Date(p.created).toLocaleDateString('zh-CN')}</small></span><span>›</span></button>)}</nav>
      <div className="sidebar-bottom"><span className={'connection '+(health.ok?'online':'')}></span>{health.ok?'本地服务已连接':'连接本地服务中'}<small>{health.model||'模型未连接'} · 单任务执行</small><div className="sidebar-version">LOCAL FIRST <span>v0.1</span></div></div>
    </aside>
    <main><header className="topbar"><span>工作空间 <b>/</b> {project?.name||'开始一个新项目'}</span><span className="local-label">● 本机运行 · 源码保存在你的文件夹</span></header>
    <div className="page-title"><div><div className="eyebrow">IDEA → BUILD → VERIFY</div><h1>{project?project.name:'把想法，交给工序。'}</h1><p>{project?project.path:'描述需求，确认方案，看三个 Agent 将它变成可运行的软件。'}</p></div><div className={'run-status '+(run?.status||'idle')}><i></i>{run?.status==='completed'&&!run.report_current?'源码已变更，需重新检查':run?labels[run.status]||run.status:'等待开始'}<small>{run?'RUN '+run.id.slice(0,8).toUpperCase():'选择一个空文件夹作为起点'}</small></div></div>
    {error&&<div className="error-banner" role="alert"><span>{error}</span><button onClick={()=>setError('')}>关闭</button></div>}
    {create&&<section className="create-panel"><div><div className="eyebrow">NEW PROJECT</div><h2>选择开发位置</h2><p>选择一个已经存在的空文件夹。源码将直接写入这里。</p></div><div className="create-fields"><input aria-label="项目名称" placeholder="项目名称，例如：我的番茄钟" value={name} onChange={e=>setName(e.target.value)}/><div className="path-row"><input aria-label="开发目录" placeholder="D:\Projects\my-app" value={path} onChange={e=>setPath(e.target.value)}/><button className="secondary" disabled={busy} onClick={()=>action(async()=>{const v=await api('/pick-directory',{});if(v.path)setPath(v.path);})}>选择文件夹</button></div><div className="row-actions"><button className="text-button" onClick={()=>setCreate(false)}>取消</button><button className="primary" disabled={busy||!path.trim()} onClick={()=>action(async()=>{const p=await api<Project>('/projects',{path,name:name.trim()||'未命名项目'});await loadProjects();await openProject(p.id);setPath('');setName('');})}>建立项目 ↗</button></div></div></section>}
    {!project?<section className="welcome"><div className="welcome-number">01<span>从一个文件夹开始</span></div><h2>你提需求。<br/>它们协作完成。</h2><div className="welcome-flow"><div><b>01</b>需求 Agent<small>整理方案，等待你确认</small></div><span>→</span><div><b>02</b>编码 Agent<small>写入源码，启动应用</small></div><span>→</span><div><b>03</b>审查 Agent<small>实际操作，逐项验收</small></div></div><button className="primary" onClick={()=>setCreate(true)}>新建第一个项目 ↗</button><p className="welcome-note">单页应用、多页面前端、带后端与数据库的应用</p></section>:<div className="workbench">
      <section className="requirements-panel"><div className="panel-heading"><span>01 / 需求与方案</span><small>你决定做什么</small></div><div className="panel-scroll">
        {run&&<div className="user-message"><span>你的需求</span><p>{run.request}</p></div>}
        {run?.status==='waiting_confirmation'&&run.state.plan?<PlanEditor run={run} busy={busy} onDecision={decide}/>:run?.state.plan?<div className="confirmed-plan"><div className="section-label">已整理的方案 <span>REV {run.state.plan_revision}</span></div><h3>{run.state.plan.title}</h3><p>{run.state.plan.summary}</p><ul>{run.state.plan.features.map((s:string,i:number)=><li key={i}>{s}</li>)}</ul><div className="stack-note">{run.state.plan.tech_stack}</div><div className="section-label">页面结构</div><p>{run.state.plan.pages.join(' / ')}</p></div>:null}
        {run?.state.error&&<div className="inline-error">{run.state.error}</div>}
        {run?.status==='waiting_repair'&&<div className="repair-panel"><h3>本批三轮修复已结束</h3><p>当前结果保留在本地。你可以继续一批修复，或调整需求。</p><ul>{run.state.issues?.map((v:string,i:number)=><li key={i}>{v}</li>)}</ul><button className="primary full" disabled={busy} onClick={()=>decide('continue')}>继续修复 · 最多三轮</button><textarea value={repairFeedback} onChange={e=>setRepairFeedback(e.target.value)} placeholder="也可以补充修改需求…"/><button className="secondary full" disabled={busy||!repairFeedback.trim()} onClick={()=>decide('revise',undefined,repairFeedback)}>重新整理需求</button><button className="text-button" disabled={busy} onClick={()=>decide('end')}>结束本次任务</button></div>}
        {run&&['failed','stopped','interrupted'].includes(run.status)&&<button className="secondary full" disabled={busy} onClick={()=>action(async()=>setRun(await api<Run>('/runs/'+run.id+'/retry',{})))}>从当前源码手动重试</button>}
        {run&&active&&<button className="stop-button" disabled={busy||run.status==='stopping'} onClick={()=>action(async()=>setRun(await api<Run>('/runs/'+run.id+'/stop',{})))}>■ {run.status==='stopping'?'正在清理执行…':'停止当前任务'}</button>}
        {run?.status==='waiting_confirmation'&&<button className="text-button" disabled={busy} onClick={()=>decide('end')}>结束，不开始开发</button>}
      </div><div className="composer"><label htmlFor="request">{run?'继续提出修改需求':'你想开发什么？'}</label><textarea id="request" rows={4} disabled={Boolean(run&&!terminal.has(run.status))} placeholder="例如：做一个简洁的番茄钟，支持开始、暂停和重置。" value={request} onChange={e=>setRequest(e.target.value)}/><button className="primary full" disabled={busy||!request.trim()||Boolean(run&&!terminal.has(run.status))} onClick={()=>action(async()=>{setRun(await api<Run>('/projects/'+project.id+'/runs',{request}));setRequest('');})}>整理需求 <span>↗</span></button><small>编码前会展示方案，等待你确认。</small></div></section>
      <section className="execution-panel"><div className="panel-heading"><span>02 / 协作进度</span><small>真实执行记录</small></div><div className="stage-list">{['planner','coder','reviewer'].map((s,i)=><div key={s} className={'stage '+((run?.phase===s||run?.phase===phaseFor[s])&&active?'current':'')}><span>{String(i+1).padStart(2,'0')}</span><div>{labels[s]}<small>{s==='planner'?'理解与确认':s==='coder'?'文件与运行':'检查与反馈'}</small></div></div>)}</div><div className="round-counter"><span>修复批次 <b>{run?.state.batch_id||1}</b></span><span>本批 <b>{run?.state.repair_count||0}<em>/3</em></b></span></div><div className="event-list">{events.length?events.map(e=><details key={e.id} className={'event '+(e.type.includes('failed')?'event-error':'')}><summary><time>{clock(e.created)}</time><span>{eventTitle(e)}</span></summary><pre>{JSON.stringify(e.payload,null,2)}</pre></details>):<div className="event-empty">执行开始后，<br/>角色交接与工具结果会出现在这里。</div>}</div></section>
      <section className="output-panel"><div className="output-tabs"><div>{[['preview','应用预览'],['code','项目源码'],['report','检查报告'],['history','执行历史']].map(([id,label])=><button className={tab===id?'active':''} key={id} onClick={()=>setTab(id)}>{label}{id==='report'&&expected>0&&<span>{passed}/{expected}</span>}</button>)}</div><span className="output-tag">03 / OUTPUT</span></div>
        {tab==='preview'&&<><div className="preview-toolbar"><span>{preview||'应用尚未启动'}</span><div><button className="text-button" disabled={busy||Boolean(active)} onClick={()=>action(async()=>{const v=await api('/projects/'+project.id+'/preview',{});setPreview(v.preview_url);setFrameKey(k=>k+1);})}>启动预览</button><button className="text-button" disabled={!preview} onClick={()=>setFrameKey(k=>k+1)}>刷新</button>{preview&&<a href={preview} target="_blank" rel="noreferrer">独立打开 ↗</a>}<button className="text-button" disabled={busy||Boolean(active)||!preview} onClick={()=>action(async()=>{await api('/projects/'+project.id+'/preview',undefined,'DELETE');setPreview('');})}>停止预览</button></div></div>{preview?<iframe title="生成应用预览" key={frameKey+preview} src={preview} sandbox="allow-scripts allow-forms allow-same-origin allow-modals allow-downloads"/>:<div className="output-empty"><div className="preview-outline"><span></span><span></span><span></span><div>YOUR NEXT APP</div></div><h3>成果将在这里运行</h3><p>确认需求后，Agent 会写入源码、启动应用并实际检查。</p><small>每个“通过”，都需要对应的执行证据。</small></div>}</>}
        {tab==='code'&&<div className="code-view"><div className="file-list">{fileList.map(f=><button key={f} className={f===selectedFile?'selected':''} onClick={()=>setSelectedFile(f)}>{f}</button>)}{!fileList.length&&<p>还没有生成文件</p>}</div><div className="code-body"><div className="code-filename">{selectedFile||'选择一个源码文件'}</div><pre>{code||'源码会直接保存在你选定的本地文件夹。'}</pre></div></div>}
        {tab==='report'&&<div className="report-view"><div className="report-intro">{report?.results?.length>0&&!run?.report_current&&<div className="inline-error">当前源码与该报告不一致。以下仅为历史检查结果，不能代表当前源码通过。</div>}<div className="eyebrow">VERIFICATION REPORT</div><h2>{passed}<span> / {expected} 项通过</span></h2><p>{report?.summary||'确认验收清单后，检查结果会在这里逐项呈现。'}</p><small>报告属于执行 {run?.id.slice(0,8)||'—'}；修改后需要重新检查。</small></div>{report?.results?.map((r:any)=><article className={'check-row '+r.status} key={r.criterion_id}><span className="check-icon">{r.status==='passed'?'✓':r.status==='failed'?'×':'—'}</span><div><div className="check-title">{run?.state.plan?.acceptance?.find((c:Criterion)=>c.id===r.criterion_id)?.description||r.criterion_id}<b>{r.status==='passed'?'通过':r.status==='failed'?'失败':'未执行'}</b></div><p>{r.detail}</p><details><summary>{r.evidence_ids.length} 条工具证据</summary>{run&&<Evidence runId={run.id} ids={r.evidence_ids}/>}</details></div></article>)}</div>}
        {tab==='history'&&<div className="history-view"><h2>每一次执行，都有记录。</h2><button className="text-button" onClick={()=>action(async()=>{const p=await api<Project>('/projects/'+project.id);setProject(p);})}>刷新记录</button>{project.runs?.map(r=><button className={'history-row '+(r.id===run?.id?'selected':'')} key={r.id} onClick={()=>{setRun(r);setTab('report');}}><div><b>{r.request.slice(0,75)}</b><small>{new Date(r.created).toLocaleString('zh-CN')} · {r.id.slice(0,8)}</small></div><span>{labels[r.status]||r.status} →</span></button>)}</div>}
      </section>
    </div>}
    <footer><span>工序 · 把协作过程看清楚。</span><span>LANGGRAPH / LANGCHAIN / PLAYWRIGHT</span></footer></main>
  </div>;
}

createRoot(document.getElementById('root')!).render(<React.StrictMode><App/></React.StrictMode>);
