import { useEffect, useState } from "react";
import { invoke } from "@tauri-apps/api/core";
import { OfficeView } from "../office/OfficeView";
import { useWorldStore } from "../stores/worldStore";
import { useOpenLogStore } from "../stores/openLogStore";
import { api } from "../ipc/commands";
import type { Session } from "../bindings";
import "./hq.css";
import { HqHistory, type History } from "./HqHistory";
import { HqOperations } from "./HqOperations";
import type { TurnTelemetry } from "./HqTurns";
import {tokenValue} from "./tokenValue";

type Usage = { context:number; requests:number; prompt:string; last_event:string; totals:Record<string,number>; last:Record<string,number>|null };
export type Worker = { alive:boolean; busy:boolean; turns:number; cwd:string; session:string; usage?:Usage; last_activity?:number };
export type Task = { id:string; project:string; prompt:string; status:string; started:number; finished?:number; steps:number; error?:string };
export type Snapshot = { workers:Record<string,Worker>; tasks:Task[]; history?:History; history_error?:string; tasks_error?:string; office?:TurnTelemetry; office_error?:string };
const n = (value?:number) => new Intl.NumberFormat("ru-RU", {notation:"compact", maximumFractionDigits:1}).format(value || 0);
const age = (ts?:string) => ts ? `${Math.max(0,Math.floor((Date.now()-Date.parse(ts))/60000))} мин назад` : "нет событий";

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
        const data = await invoke<Snapshot>("get_hq_snapshot");
        if (disposed) return;
        setSnapshot(data); setError(""); setUpdated(new Date().toLocaleTimeString("ru-RU")); setCheckedAt(Date.now());
        const initial = await api.getInitialState().catch(e=>{if(!disposed)setLogError(String(e));return null;});
        if (disposed || !initial) return;
        setLogError("");
        const sessions:Session[] = Object.entries(data.workers).filter(([,w])=>w.session).map(([name,w])=> {
          const original = initial.sessions.find(s=>s.session_id===w.session);
          return {
            ...original, session_id:w.session, project:w.cwd, slug:name=== "hq" ? "HQ · Главный" : name,
            git_branch:original?.git_branch ?? null, status:!w.alive?"Ended":w.busy?"Active":"Idle",
            started_at:original?.started_at ?? null, last_event_at:w.usage?.last_event ?? original?.last_event_at ?? null,
            current:!w.busy ? {kind:"Idle",tool_name:null,detail:null,since:null,active_skill:null,todos:[]} : original?.current ?? {kind:"Thinking",tool_name:null,detail:null,since:null,active_skill:null,todos:[]},
            is_main:true, subagents:original?.subagents ?? [],
          };
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
  const tasks=(snapshot?.tasks ?? []).filter(t=>t.project===selected).sort((a,b)=>b.started-a.started).slice(0,12);
  return <div className="hq-layout">
    <div className="hq-bar"><strong>Операционный центр</strong><span className={error?"hq-error":"hq-live"}>{error?"HQ недоступен · данные устарели":snapshot?"● Подключено к HQ":"Подключаемся…"}</span><span>{entries.filter(([,w])=>w.busy).length} работают / {entries.length} воркеров</span><small>Обновлено {updated || "—"}</small></div>
    {error && <div className="hq-error">{error}</div>}
    {logError && <div className="hq-error">Журналы недоступны: {logError}</div>}
    <nav className="hq-switch"><button className={view==="operations"?"active":""} onClick={()=>setView("operations")}>Диспетчер</button><button className={view==="office"?"active":""} onClick={()=>setView("office")}>Команда и офис</button><button className={view==="history"?"active":""} onClick={()=>setView("history")}>История и аналитика</button>{snapshot?.history_error && <span className="hq-error">История не сохраняется: {snapshot.history_error}</span>}{snapshot?.tasks_error && <span className="hq-error">Задачи временно недоступны</span>}</nav>
    {view==="operations" ? <HqOperations snapshot={snapshot} offline={!!error} now={monitor.checkedAt} onSelect={name=>{setSelected(name);setView("office");}}/> : view==="history" ? <HqHistory history={snapshot?.history} error={snapshot?.history_error}/> : <div className="hq-body"><div className="hq-canvas"><OfficeView focusSessionId={worker?.session}/></div><aside className="hq-panel">
      <h3>Команда</h3>
      {entries.map(([name,w])=><button className={`hq-worker ${selected===name?"selected":""}`} key={name} onClick={()=>setSelected(name)}><strong>{name}</strong><span>{!w.alive?"Остановлен":w.busy?"В работе":"Ожидает поручения"}</span><small>Контекст {n(w.usage?.context)} · {age(w.usage?.last_event)}</small></button>)}
      {worker && <><h3>{selected} / текущая сессия</h3><p className="hq-path">{worker.cwd}</p><p className="hq-prompt">{worker.usage?.prompt || "Поручение ещё не записано"}</p>
      <p>{worker.busy ? `${session?.current.tool_name || session?.current.kind || "Работает"}: ${session?.current.detail || "ожидаем следующее событие"}` : "Воркер свободен"}</p>
      <p className="hq-muted">Ходов процесса: {worker.turns} · субагентов в журнале: {session?.subagents.length ?? 0}. Обновление каждые 5 секунд.</p>
      <button onClick={()=>useOpenLogStore.getState().open({sessionId:worker.session,agentId:null,title:selected},{x:40,y:120})}>Открыть журнал действий</button>
      <h3>Контекст последнего запроса</h3><div className="hq-number">{n(worker.usage?.context)} <small>токенов</small></div>
      <p className="hq-muted">Вход по последней записи usage, включая кеш. Не размер доступного окна и не оставшийся лимит.</p>
      <div className="hq-stats">{[["Новый ввод","input_tokens"],["Запись кеша","cache_creation_input_tokens"],["Чтение кеша","cache_read_input_tokens"],["Ответ","output_tokens"]].map(([label,key])=><div key={key}><span>{label}</span><strong>{tokenValue(worker.usage?.last,key)}</strong></div>)}</div>
      <h3>Накоплено за сессию</h3><p>{worker.usage?.requests ?? 0} уникальных ответов API · output {n(worker.usage?.totals.output_tokens)}</p><p className="hq-muted">Чтение кеша {n(worker.usage?.totals.cache_read_input_tokens)} · запись {n(worker.usage?.totals.cache_creation_input_tokens)}. Это объём обработки, не остаток подписки.</p>
      <h3>Задачи проекта</h3>{tasks.length===0?<p className="hq-muted">HQ не вернул задач этого проекта.</p>:tasks.map(t=><details key={t.id}><summary>{t.status} · {t.steps} шагов · {t.id}</summary><p>{t.prompt}</p></details>)}
      </>}
    </aside></div>}
  </div>;
}
