import { useEffect, useState } from "react";
import { OfficeView } from "../office/OfficeView";
import { useWorldStore } from "../stores/worldStore";
import { useOpenLogStore } from "../stores/openLogStore";
import { api } from "../ipc/commands";
import type { HqSnapshot, Session } from "../bindings";
import "./hq.css";
import { HqHistory, type History } from "./HqHistory";
import { HqOperations } from "./HqOperations";
import {tokenValue} from "./tokenValue";
import { currentLocale, intlTag, t } from "../i18n";

export type Worker = HqSnapshot["workers"][string];
export type Task = HqSnapshot["tasks"][number];
export type Snapshot = HqSnapshot & { history?: History };
const n = (value?:number) => new Intl.NumberFormat(intlTag(currentLocale()), {notation:"compact", maximumFractionDigits:1}).format(value || 0);
const age = (ts?:string) => ts ? t("hq.team.minutesAgo", {n: Math.max(0,Math.floor((Date.now()-Date.parse(ts))/60000))}) : t("hq.team.noEvents");

export function useHqMonitor() {
  const [snapshot,setSnapshot] = useState<Snapshot|null>(null);
  const [error,setError] = useState("");
  const [updated,setUpdated] = useState("");
  const [logError,setLogError] = useState("");
  const [checkedAt,setCheckedAt] = useState(0);
  useEffect(() => {
    let disposed = false;
    let timer:ReturnType<typeof setTimeout>;
    async function refresh() {
      try {
        const data = await api.getHqSnapshot();
        if (disposed) return;
        setSnapshot(data); setError(""); setUpdated(new Date().toLocaleTimeString(intlTag(currentLocale()))); setCheckedAt(Date.now());
        const initial = await api.getInitialState().catch(e=>{if(!disposed)setLogError(String(e));return null;});
        if (disposed || !initial) return;
        setLogError("");
        const sessions:Session[] = Object.entries(data.workers).flatMap(([name,w])=> {
          if (!w.session) return [];
          const original = initial.sessions.find(s=>s.session_id===w.session);
          return [{
            ...original, session_id:w.session, project:w.cwd, slug:name=== "hq" ? t("hq.team.mainSlug") : name,
            git_branch:original?.git_branch ?? null, status:!w.alive?"Ended":w.busy?"Active":"Idle",
            started_at:original?.started_at ?? null, last_event_at:w.usage?.last_event ?? original?.last_event_at ?? null,
            current:!w.busy ? {kind:"Idle",tool_name:null,detail:null,since:null,active_skill:null,todos:[]} : original?.current ?? {kind:"Thinking",tool_name:null,detail:null,since:null,active_skill:null,todos:[]},
            is_main:true, subagents:original?.subagents ?? [],
          }];
        });
        useWorldStore.setState({sessions,agents:initial.agents,loaded:true});
      } catch(e) { if(!disposed) setError(String(e)); }
      finally { if(!disposed) timer=setTimeout(refresh,5000); }
    }
    refresh();
    return ()=>{disposed=true;clearTimeout(timer);};
  },[]);
  return {snapshot,error,updated,logError,checkedAt};
}

export function HqOffice({monitor}:{monitor:ReturnType<typeof useHqMonitor>}) {
  const {snapshot,error,updated,logError}=monitor;
  const [selected,setSelected]=useState("hq");
  const [view,setView]=useState<"office"|"history"|"operations">("operations");
  const sessions=useWorldStore(s=>s.sessions);
  const entries=Object.entries(snapshot?.workers ?? {});
  const worker=snapshot?.workers[selected];
  const session=sessions.find(s=>s.session_id===worker?.session);
  const tasks=(snapshot?.tasks ?? []).filter(task=>task.project===selected).sort((a,b)=>b.started-a.started).slice(0,12);
  return <div className="hq-layout">
    <div className="hq-bar"><strong>{t("hq.bar.title")}</strong><span className={error?"hq-error":"hq-live"}>{error?t("hq.bar.unavailable"):snapshot?t("hq.bar.connected"):t("hq.bar.connecting")}</span><span>{t("hq.bar.workersStatus",{busy:entries.filter(([,w])=>w.busy).length,total:entries.length})}</span><small>{t("hq.bar.updated",{time:updated || t("hq.bar.noTime")})}</small></div>
    {error && <div className="hq-error">{error}</div>}
    {logError && <div className="hq-error">{t("hq.bar.logsUnavailable",{error:logError})}</div>}
    <nav className="hq-switch"><button className={view==="operations"?"active":""} onClick={()=>setView("operations")}>{t("hq.nav.operations")}</button><button className={view==="office"?"active":""} onClick={()=>setView("office")}>{t("hq.nav.office")}</button><button className={view==="history"?"active":""} onClick={()=>setView("history")}>{t("hq.nav.history")}</button>{snapshot?.history_error && <span className="hq-error">{t("hq.nav.historyUnavailable",{error:snapshot.history_error})}</span>}{snapshot?.tasks_error && <span className="hq-error">{t("hq.nav.tasksUnavailable")}</span>}</nav>
    {view==="operations" ? <HqOperations snapshot={snapshot} offline={!!error} now={monitor.checkedAt} onSelect={name=>{setSelected(name);setView("office");}}/> : view==="history" ? <HqHistory history={snapshot?.history} error={snapshot?.history_error}/> : <div className="hq-body"><div className="hq-canvas"><OfficeView focusSessionId={worker?.session ?? undefined}/></div><aside className="hq-panel">
      <h3>{t("hq.team.heading")}</h3>
      {entries.map(([name,w])=><button className={`hq-worker ${selected===name?"selected":""}`} key={name} onClick={()=>setSelected(name)}><strong>{name}</strong><span>{!w.alive?t("hq.team.stopped"):w.busy?t("hq.team.busy"):t("hq.team.idle")}</span><small>{t("hq.team.contextLabel",{value:n(w.usage?.context)})} · {age(w.usage?.last_event)}</small></button>)}
      {worker && <><h3>{t("hq.session.heading",{name:selected})}</h3><p className="hq-path">{worker.cwd}</p><p className="hq-prompt">{worker.usage?.prompt || t("hq.session.noPrompt")}</p>
      <p>{worker.busy ? `${session?.current.tool_name || session?.current.kind || t("hq.session.workingFallback")}: ${session?.current.detail || t("hq.session.waitingNextEvent")}` : t("hq.session.free")}</p>
      <p className="hq-muted">{t("hq.session.turnsCount",{turns:worker.turns,count:session?.subagents.length ?? 0})}</p>
      {worker.session && (() => {
        const sessionId = worker.session;
        return <button onClick={()=>useOpenLogStore.getState().open({sessionId,agentId:null,title:selected},{x:40,y:120})}>{t("hq.session.openLog")}</button>;
      })()}
      <h3>{t("hq.session.contextHeading")}</h3><div className="hq-number">{n(worker.usage?.context)} <small>{t("hq.session.tokens")}</small></div>
      <p className="hq-muted">{t("hq.session.contextNote")}</p>
      <div className="hq-stats">{[[t("hq.tokens.input"),"input_tokens"],[t("hq.tokens.cacheWrite"),"cache_creation_input_tokens"],[t("hq.tokens.cacheRead"),"cache_read_input_tokens"],[t("hq.tokens.output"),"output_tokens"]].map(([label,key])=><div key={key}><span>{label}</span><strong>{tokenValue(worker.usage?.last,key)}</strong></div>)}</div>
      <h3>{t("hq.session.accumulatedHeading")}</h3><p>{t("hq.session.uniqueResponses",{count:worker.usage?.requests ?? 0,output:n(worker.usage?.totals.output_tokens)})}</p><p className="hq-muted">{t("hq.session.cacheNote",{read:n(worker.usage?.totals.cache_read_input_tokens),write:n(worker.usage?.totals.cache_creation_input_tokens)})}</p>
      <h3>{t("hq.session.tasksHeading")}</h3>{tasks.length===0?<p className="hq-muted">{t("hq.session.noProjectTasks")}</p>:tasks.map(task=><details key={task.id}><summary>{t("hq.session.taskSummary",{status:task.status,steps:task.steps,id:task.id})}</summary><p>{task.prompt}</p></details>)}
      </>}
    </aside></div>}
  </div>;
}
