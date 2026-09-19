import { useState } from "react";
import { currentLocale, intlTag, t } from "../i18n";

type Day = Record<string,number>;
type Task = {id:string;project:string;prompt?:string;status:string;steps?:number;started?:number;finished?:number};
type Event = {at:string;project:string;kind:string;id?:string;status?:string;initial?:boolean;before?:{session?:string};after?:{alive:boolean;busy:boolean;session:string}};
export type History = {since:string;sessions:Record<string,{project:string;days:Record<string,Day>}>;tasks:Record<string,Task>;events:Event[]};
const format=(n:number)=>new Intl.NumberFormat(intlTag(currentLocale()),{maximumFractionDigits:0}).format(n);
const date=(s:string)=>new Date(s).toLocaleString(intlTag(currentLocale()));
const statuses=():Record<string,string>=>({running:t("hq.taskStatus.running"),done:t("hq.taskStatus.done"),error:t("hq.taskStatus.error"),cancelled:t("hq.taskStatus.cancelled")});
export function aggregateDays(history:History,project:string):[string,Day][] {
  const days:Record<string,Day>={};
  for(const session of Object.values(history.sessions)) {
    if(project && session.project!==project) continue;
    for(const [date,values] of Object.entries(session.days)) {
      const day=days[date]??={};
      for(const [key,value] of Object.entries(values)) day[key]=(day[key]||0)+value;
    }
  }
  return Object.entries(days).sort(([a],[b])=>b.localeCompare(a));
}
export function HqHistory({history,error}:{history?:History;error?:string}) {
  const [project,setProject]=useState("");
  const [period,setPeriod]=useState(7);
  if(!history) return <div className="hq-history">{error || t("hq.history.loading")}</div>;
  const st=statuses();
  const projects=[...new Set([...Object.values(history.sessions).map(s=>s.project),...Object.values(history.tasks).map(task=>task.project)])].sort();
  const cutoff=new Date(); cutoff.setUTCDate(cutoff.getUTCDate()-(period-1));
  const days=aggregateDays(history,project).filter(([d])=>!period || d>=cutoff.toISOString().slice(0,10));
  const totals:Day={}; for(const [,day] of days) for(const [key,value] of Object.entries(day)) totals[key]=(totals[key]||0)+value;
  const tasks=Object.values(history.tasks).filter(task=>!project || task.project===project).sort((a,b)=>(b.started||0)-(a.started||0));
  const events=history.events.filter(e=>!project || e.project===project).slice(-100).reverse();
  const fields=[[t("hq.tokens.input"),"input_tokens"],[t("hq.tokens.cacheWrite"),"cache_creation_input_tokens"],[t("hq.tokens.cacheRead"),"cache_read_input_tokens"],[t("hq.tokens.output"),"output_tokens"]];
  return <div className="hq-history">
    <header><div><h2>{t("hq.history.title")}</h2><p>{t("hq.history.observedSince",{date:date(history.since)})}</p></div><select aria-label={t("hq.history.projectLabel")} value={project} onChange={e=>setProject(e.target.value)}><option value="">{t("hq.history.allProjects")}</option>{projects.map(p=><option key={p}>{p}</option>)}</select><select aria-label={t("hq.history.periodLabel")} value={period} onChange={e=>setPeriod(Number(e.target.value))}><option value={7}>{t("hq.history.period7")}</option><option value={30}>{t("hq.history.period30")}</option><option value={0}>{t("hq.history.periodAll")}</option></select></header>
    {error && <p className="hq-error">{error}</p>}
    <div className="hq-history-stats">{fields.map(([label,key])=><div key={key}><span>{label}</span><strong>{format(totals[key]||0)}</strong></div>)}<div><span>{t("hq.history.apiResponses")}</span><strong>{format(totals.requests||0)}</strong></div></div>
    <p className="hq-muted">{t("hq.history.note")}</p>
    <h3>{t("hq.history.dailyHeading")}</h3><div className="hq-table-wrap"><table><thead><tr><th>{t("hq.history.dayColumn")}</th>{fields.map(([label])=><th key={label}>{label}</th>)}<th>API</th></tr></thead><tbody>{days.map(([d,day])=><tr key={d}><td>{d}</td>{fields.map(([,key])=><td key={key}>{format(day[key]||0)}</td>)}<td>{format(day.requests||0)}</td></tr>)}</tbody></table>{!days.length && <p>{t("hq.history.noRecordsPeriod")}</p>}</div>
    <div className="hq-history-columns"><section><h3>{t("hq.history.tasksHeading",{count:tasks.length})}</h3><p className="hq-muted">{t("hq.history.tasksNote")}</p>{tasks.map(task=><details key={task.id}><summary><span className={`hq-status ${task.status}`}>{st[task.status]||task.status}</span> {task.project} · {task.id}</summary><p>{task.prompt || t("hq.history.noTaskPrompt")}</p><small>{t("hq.history.started",{value:task.started?date(new Date(task.started*1000).toISOString()):t("hq.history.notSpecified")})} · {t("hq.history.stepsLabel",{value:task.steps??"—"})}{task.finished&&task.started?t("hq.history.durationSec",{sec:Math.max(0,Math.round(task.finished-task.started))}):t("hq.history.durationUnknown")}</small></details>)}{!tasks.length && <p>{t("hq.history.noTasksYet")}</p>}</section>
    <section><h3>{t("hq.history.feedHeading")}</h3><p className="hq-muted">{t("hq.history.feedNote")}</p>{events.map((e,i)=><div className="hq-event" key={`${e.at}-${i}`}><small>{date(e.at)} · {e.project}</small><p>{e.kind==="missing"?t("hq.history.workerMissing"):e.kind==="task"?`${e.initial?t("hq.history.taskDiscovered"):t("hq.history.taskStatus")}: ${e.id} — ${st[e.status||""]||e.status}`:`${e.kind==="discovered"?t("hq.history.workerDiscovered"):e.before?.session!==e.after?.session?t("hq.history.sessionChanged"):t("hq.history.stateChanged")}: ${!e.after?.alive?t("hq.history.stopped"):e.after.busy?t("hq.history.workingLower"):t("hq.history.waiting")}`}</p></div>)}</section></div>
  </div>;
}
