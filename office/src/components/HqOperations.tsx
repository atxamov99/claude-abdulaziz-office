import type { Snapshot, Worker } from "./HqOffice";
import {HqTurns} from "./HqTurns";
export function workerSignal(worker:Worker,now:number,offline:boolean) {
  if(offline) return {tone:"warn",title:"Данные устарели",detail:"Нет свежего подтверждения от HQ"};
  if(!worker.alive) return {tone:"error",title:"Недоступен",detail:"HQ сообщает, что процесс остановлен"};
  if(!worker.busy) return {tone:"ok",title:"Свободен",detail:"Ожидает поручения"};
  const at=worker.last_activity ? worker.last_activity*1000 : Date.parse(worker.usage?.last_event || "");
  if(!Number.isFinite(at)) return {tone:"warn",title:"Работает · нет журнала",detail:"Нельзя оценить последнее действие"};
  const minutes=Math.max(0,Math.floor((now-at)/60000));
  if(minutes>=10) return {tone:"warn",title:"Давно нет событий",detail:`Последняя запись ${minutes} мин назад. Возможно, выполняется долгий инструмент; зависание не подтверждено.`};
  return {tone:"ok",title:"Работает",detail:`Последняя запись ${minutes} мин назад`};
}
export function HqOperations({snapshot,offline,now,onSelect}:{snapshot:Snapshot|null;offline:boolean;now:number;onSelect:(name:string)=>void}) {
  if(!snapshot) return <div className="hq-history">{offline?"HQ недоступен. Ожидаем восстановления связи.":"Загрузка диспетчера…"}</div>;
  const workers=Object.entries(snapshot.workers);
  const tasks=snapshot.tasks||[];
  const running=tasks.filter(t=>t.status==="running");
  const errors=tasks.filter(t=>t.status==="error").sort((a,b)=>b.started-a.started);
  return <div className="hq-history"><h2>Диспетчер HQ</h2><p className="hq-muted">Наблюдение без управляющих команд. История собирается на всех вкладках, пока работает приложение; при закрытии или сне компьютера сбор прекращается.</p>
    <div className="hq-history-stats"><div><span>Работают {offline?"(старый снимок)":""}</span><strong>{workers.filter(([,w])=>w.alive&&w.busy).length}</strong></div><div><span>Свободны</span><strong>{workers.filter(([,w])=>w.alive&&!w.busy).length}</strong></div><div><span>Недоступны</span><strong>{workers.filter(([,w])=>!w.alive).length}</strong></div><div><span>Фоновые задачи</span><strong>{snapshot.tasks_error?"—":running.length}</strong></div><div><span>Очередь ходов + склейка TG</span><strong style={{fontSize:18}}>{snapshot.office&&!snapshot.office.error?`${snapshot.office.waiting} + ${snapshot.office.buffering.length}`:"Недоступна"}</strong></div></div>
    <div className="hq-operator-workers">{workers.map(([name,w])=>{const signal=workerSignal(w,now,offline);return <button key={name} className={`hq-operator-card ${signal.tone}`} onClick={()=>onSelect(name)}><strong>{name}</strong><b>{signal.title}</b><span>{signal.detail}</span><p>{w.usage?.prompt || "Текст поручения отсутствует"}</p><small>Открыть карточку и журнал →</small></button>;})}</div>
    <HqTurns data={snapshot.office} error={snapshot.office_error}/>
    <h3>Активные фоновые задачи</h3>{snapshot.tasks_error?<p className="hq-error">API задач недоступен. Отсутствие задач не подтверждено.</p>:!running.length?<p className="hq-muted">В последнем снимке активных фоновых задач нет. Работа из Telegram может выполняться отдельно от этого списка.</p>:running.map(t=><details key={t.id}><summary>{t.project} · {t.id} · {t.steps} шагов</summary><p>{t.prompt}</p>{!snapshot.workers[t.project]?.busy&&<p className="hq-error">Статус задачи расходится с состоянием воркера. Проверьте журнал; автоматически задача не завершена.</p>}</details>)}
    <h3>Ошибки в доступной истории задач · {errors.length}</h3><p className="hq-muted">Это исторические ошибки, не обязательно текущие аварии. Ошибки после последующего успешного запуска остаются здесь для проверки.</p>{errors.slice(0,20).map(t=><details key={t.id}><summary><span className="hq-status error">Ошибка</span> · {t.project} · {t.id} · {new Date(t.started*1000).toLocaleString("ru-RU")}</summary><p>{t.error || "HQ не предоставил причину"}</p><p>{t.prompt}</p></details>)}{!errors.length&&<p className="hq-muted">В доступном снимке ошибок нет.</p>}
  </div>;
}
