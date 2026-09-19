import type { Snapshot, Worker } from "./HqOffice";
import {HqTurns} from "./HqTurns";
import { currentLocale, intlTag, t } from "../i18n";
export function workerSignal(worker:Worker,now:number,offline:boolean) {
  if(offline) return {tone:"warn",title:t("hq.workerSignal.staleTitle"),detail:t("hq.workerSignal.staleDetail")};
  if(!worker.alive) return {tone:"error",title:t("hq.workerSignal.unavailableTitle"),detail:t("hq.workerSignal.unavailableDetail")};
  if(!worker.busy) return {tone:"ok",title:t("hq.workerSignal.freeTitle"),detail:t("hq.workerSignal.freeDetail")};
  const at=worker.last_activity ? worker.last_activity*1000 : Date.parse(worker.usage?.last_event || "");
  if(!Number.isFinite(at)) return {tone:"warn",title:t("hq.workerSignal.noLogTitle"),detail:t("hq.workerSignal.noLogDetail")};
  const minutes=Math.max(0,Math.floor((now-at)/60000));
  if(minutes>=10) return {tone:"warn",title:t("hq.workerSignal.staleEventsTitle"),detail:t("hq.workerSignal.staleEventsDetail",{min:minutes})};
  return {tone:"ok",title:t("hq.workerSignal.workingTitle"),detail:t("hq.workerSignal.workingDetail",{min:minutes})};
}
export function HqOperations({snapshot,offline,now,onSelect}:{snapshot:Snapshot|null;offline:boolean;now:number;onSelect:(name:string)=>void}) {
  if(!snapshot) return <div className="hq-history">{offline?t("hq.operations.loadingOffline"):t("hq.operations.loading")}</div>;
  const workers=Object.entries(snapshot.workers);
  const tasks=snapshot.tasks||[];
  const running=tasks.filter(task=>task.status==="running");
  const errors=tasks.filter(task=>task.status==="error").sort((a,b)=>b.started-a.started);
  return <div className="hq-history"><h2>{t("hq.operations.title")}</h2><p className="hq-muted">{t("hq.operations.subtitle")}</p>
    <div className="hq-history-stats"><div><span>{t("hq.operations.working")}{offline?t("hq.operations.staleSnapshot"):""}</span><strong>{workers.filter(([,w])=>w.alive&&w.busy).length}</strong></div><div><span>{t("hq.operations.free")}</span><strong>{workers.filter(([,w])=>w.alive&&!w.busy).length}</strong></div><div><span>{t("hq.operations.unavailable")}</span><strong>{workers.filter(([,w])=>!w.alive).length}</strong></div><div><span>{t("hq.operations.backgroundTasks")}</span><strong>{snapshot.tasks_error?"—":running.length}</strong></div><div><span>{t("hq.operations.queueAndBuffer")}</span><strong style={{fontSize:18}}>{snapshot.office&&!snapshot.office.error?`${snapshot.office.waiting} + ${snapshot.office.buffering.length}`:t("hq.operations.unavailableShort")}</strong></div></div>
    <div className="hq-operator-workers">{workers.map(([name,w])=>{const signal=workerSignal(w,now,offline);return <button key={name} className={`hq-operator-card ${signal.tone}`} onClick={()=>onSelect(name)}><strong>{name}</strong><b>{signal.title}</b><span>{signal.detail}</span><p>{w.usage?.prompt || t("hq.operations.noTaskPrompt")}</p><small>{t("hq.operations.openCard")}</small></button>;})}</div>
    <HqTurns data={snapshot.office} error={snapshot.office_error}/>
    <h3>{t("hq.operations.activeBackgroundHeading")}</h3>{snapshot.tasks_error?<p className="hq-error">{t("hq.operations.tasksApiUnavailable")}</p>:!running.length?<p className="hq-muted">{t("hq.operations.noActiveTasks")}</p>:running.map(task=><details key={task.id}><summary>{t("hq.operations.stepsCount",{project:task.project,id:task.id,steps:task.steps})}</summary><p>{task.prompt}</p>{!snapshot.workers[task.project]?.busy&&<p className="hq-error">{t("hq.operations.statusMismatch")}</p>}</details>)}
    <h3>{t("hq.operations.errorsHeading",{count:errors.length})}</h3><p className="hq-muted">{t("hq.operations.errorsNote")}</p>{errors.slice(0,20).map(task=><details key={task.id}><summary><span className="hq-status error">{t("hq.operations.errorBadge")}</span> · {task.project} · {task.id} · {new Date(task.started*1000).toLocaleString(intlTag(currentLocale()))}</summary><p>{task.error || t("hq.operations.noReason")}</p><p>{task.prompt}</p></details>)}{!errors.length&&<p className="hq-muted">{t("hq.operations.noErrors")}</p>}
  </div>;
}
