import {useWorldStore} from "../stores/worldStore";
type Turn={id:string;project:string;source:string;task_id?:string;prompt?:string;status:string;queued:number;started?:number;finished?:number;tool?:string;tools:number;requests:number;usage:Record<string,number>;reason?:string};
export type TurnTelemetry={waiting:number;active:number;buffering:{project:string;messages:number}[];turns:Turn[];background:{id:string;project:string;status:string;description:string;last_event:number}[];error?:string};
const labels:Record<string,string>={queued:"В очереди",running:"Выполняется",finished:"Ход завершён",failed:"Ошибка",interrupted:"Прерван перезапуском"};
const n=(v?:number)=>new Intl.NumberFormat("ru-RU").format(v||0);
export function HqTurns({data,error}:{data?:TurnTelemetry;error?:string}) {
  const sessions=useWorldStore(s=>s.sessions);
  const agents=sessions.flatMap(s=>s.subagents.map(a=>({...a,project:s.slug||s.project})));
  const turns=(data?.turns||[]).slice().sort((a,b)=>b.queued-a.queued);
  return <><h3>Очередь и учёт поручений HQ</h3>
    <p className="hq-muted">Запись ведёт сам бот даже при закрытом Office. Только новые ходы после обновления HQ. «Ход завершён» означает результат CLI, а не независимую проверку качества задачи. Usage — основной агент, без отдельного расхода субагентов.</p>
    {(error||data?.error)&&<p className="hq-error">{error||data?.error}</p>}
    {data?.buffering.map((b,i)=><p key={i}>Склейка Telegram-сообщений: {b.project} · {b.messages}</p>)}
    {turns.slice(0,100).map(t=><details key={t.id}><summary><span className={`hq-status ${t.status==="failed"?"error":t.status==="running"?"running":""}`}>{labels[t.status]||t.status}</span> · {t.project} · {new Date(t.queued*1000).toLocaleString("ru-RU")} · {t.tools} инструментов</summary><p>{t.prompt}</p><small>ID {t.id} · источник {t.source}{t.task_id?` · задача ${t.task_id}`:""}</small><p>{t.started?`Ожидание ${Math.max(0,Math.round(t.started-t.queued))} сек`:"Ожидает начала"}{t.started&&t.finished?` · выполнение ${Math.max(0,Math.round(t.finished-t.started))} сек`:""} · {t.tool||"инструментов нет"}</p><p>Новый ввод {n(t.usage.input_tokens)} · запись кеша {n(t.usage.cache_creation_input_tokens)} · чтение кеша {n(t.usage.cache_read_input_tokens)} · ответ {n(t.usage.output_tokens)} · API {n(t.requests)}</p>{t.reason&&<small>{t.reason}</small>}</details>)}
    {data&&!turns.length&&<p className="hq-muted">Новых поручений после обновления ещё нет.</p>}
    <h3>Субагенты из журналов Claude · {agents.length}</h3><p className="hq-muted">Состояния восстановлены по журналам, не по проверке отдельных процессов. Отсутствие событий не доказывает завершение. Вложенные агенты помечены родителем.</p>
    {agents.map((a,i)=><details key={`${a.project}-${a.agent_id}-${i}`}><summary>{a.project} → {a.description||a.subagent_type||a.agent_id} · {a.status}</summary><p>{a.current.tool_name||a.current.kind} · {a.current.detail}</p><small>Родитель: {a.parent_agent_id||"главный воркер"} · модель {a.model||"не указана"} · последнее событие {a.last_event_at?new Date(a.last_event_at).toLocaleString("ru-RU"):"неизвестно"}</small></details>)}
    {!agents.length&&<p className="hq-muted">В текущих сессиях распознанных субагентов нет.</p>}
    <h3>Фоновые события Claude</h3><p className="hq-muted">Только task_started / task_progress / task_notification, которые CLI передал HQ. Это не полный реестр cron, launchd или внешних мониторингов.</p>{data?.background.map(t=><details key={`${t.project}-${t.id}`}><summary>{t.project} · {t.status} · {t.id}</summary><p>{t.description}</p><small>{new Date(t.last_event*1000).toLocaleString("ru-RU")}</small></details>)}{!data?.background.length&&<p className="hq-muted">Фоновых событий после обновления пока нет.</p>}
  </>;
}
