import { useState } from "react";

type Day = Record<string,number>;
type Task = {id:string;project:string;prompt?:string;status:string;steps?:number;started?:number;finished?:number};
type Event = {at:string;project:string;kind:string;id?:string;status?:string;initial?:boolean;before?:{session?:string};after?:{alive:boolean;busy:boolean;session:string}};
export type History = {since:string;sessions:Record<string,{project:string;days:Record<string,Day>}>;tasks:Record<string,Task>;events:Event[]};
const format=(n:number)=>new Intl.NumberFormat("ru-RU",{maximumFractionDigits:0}).format(n);
const date=(s:string)=>new Date(s).toLocaleString("ru-RU");
const statuses:Record<string,string>={running:"В работе",done:"Готово",error:"Ошибка",cancelled:"Отменена"};
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
  if(!history) return <div className="hq-history">{error || "Загружаем историю…"}</div>;
  const projects=[...new Set([...Object.values(history.sessions).map(s=>s.project),...Object.values(history.tasks).map(t=>t.project)])].sort();
  const cutoff=new Date(); cutoff.setUTCDate(cutoff.getUTCDate()-(period-1));
  const days=aggregateDays(history,project).filter(([d])=>!period || d>=cutoff.toISOString().slice(0,10));
  const totals:Day={}; for(const [,day] of days) for(const [key,value] of Object.entries(day)) totals[key]=(totals[key]||0)+value;
  const tasks=Object.values(history.tasks).filter(t=>!project || t.project===project).sort((a,b)=>(b.started||0)-(a.started||0));
  const events=history.events.filter(e=>!project || e.project===project).slice(-100).reverse();
  const fields=[["Новый ввод","input_tokens"],["Запись кеша","cache_creation_input_tokens"],["Чтение кеша","cache_read_input_tokens"],["Ответ","output_tokens"]];
  return <div className="hq-history">
    <header><div><h2>История и аналитика HQ</h2><p>Наблюдение с {date(history.since)} · хранится локально</p></div><select aria-label="Проект" value={project} onChange={e=>setProject(e.target.value)}><option value="">Все проекты</option>{projects.map(p=><option key={p}>{p}</option>)}</select><select aria-label="Период статистики" value={period} onChange={e=>setPeriod(Number(e.target.value))}><option value={7}>7 дней UTC</option><option value={30}>30 дней UTC</option><option value={0}>Вся история UTC</option></select></header>
    {error && <p className="hq-error">{error}</p>}
    <div className="hq-history-stats">{fields.map(([label,key])=><div key={key}><span>{label}</span><strong>{format(totals[key]||0)}</strong></div>)}<div><span>Ответов API</span><strong>{format(totals.requests||0)}</strong></div></div>
    <p className="hq-muted">Токены из usage без повторов ID сообщения; дни в UTC. Только известные Office сессии HQ, без отдельных журналов субагентов. Архивные сессии сохраняют последний наблюдённый итог. Это не стоимость и не остаток подписки.</p>
    <h3>Обработка по дням</h3><div className="hq-table-wrap"><table><thead><tr><th>День UTC</th>{fields.map(([label])=><th key={label}>{label}</th>)}<th>API</th></tr></thead><tbody>{days.map(([d,day])=><tr key={d}><td>{d}</td>{fields.map(([,key])=><td key={key}>{format(day[key]||0)}</td>)}<td>{format(day.requests||0)}</td></tr>)}</tbody></table>{!days.length && <p>За этот период записей нет.</p>}</div>
    <div className="hq-history-columns"><section><h3>Задачи · {tasks.length}</h3><p className="hq-muted">Последние 500 сохранённых задач. Период выше относится к токенам. Задачи без finished не получают вымышленную длительность.</p>{tasks.map(t=><details key={t.id}><summary><span className={`hq-status ${t.status}`}>{statuses[t.status]||t.status}</span> {t.project} · {t.id}</summary><p>{t.prompt || "Нет текста поручения"}</p><small>Начало: {t.started?date(new Date(t.started*1000).toISOString()):"не указано"} · шагов {t.steps??"—"}{t.finished&&t.started?` · длительность ${Math.max(0,Math.round(t.finished-t.started))} сек`:" · длительность неизвестна"}</small></details>)}{!tasks.length && <p>Задач пока нет.</p>}</section>
    <section><h3>Лента наблюдений</h3><p className="hq-muted">Последние 100 из 1000 сохранённых изменений. Запись идёт на всех вкладках работающего приложения. Быстрые переходы между опросами, сон компьютера и время закрытого приложения не восстанавливаются.</p>{events.map((e,i)=><div className="hq-event" key={`${e.at}-${i}`}><small>{date(e.at)} · {e.project}</small><p>{e.kind==="missing"?"Воркер исчез из снимка HQ":e.kind==="task"?`${e.initial?"Обнаружена задача":"Статус задачи"}: ${e.id} — ${statuses[e.status||""]||e.status}`:`${e.kind==="discovered"?"Обнаружен воркер":e.before?.session!==e.after?.session?"Смена сессии":"Изменение состояния"}: ${!e.after?.alive?"остановлен":e.after.busy?"работает":"ожидает"}`}</p></div>)}</section></div>
  </div>;
}
