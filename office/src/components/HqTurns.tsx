import {useWorldStore} from "../stores/worldStore";
import { currentLocale, intlTag, t } from "../i18n";
type Turn={id:string;project:string;source:string;task_id?:string;prompt?:string;status:string;queued:number;started?:number;finished?:number;tool?:string;tools:number;requests:number;usage:Record<string,number>;reason?:string};
export type TurnTelemetry={waiting:number;active:number;buffering:{project:string;messages:number}[];turns:Turn[];background:{id:string;project:string;status:string;description:string;last_event:number}[];error?:string};
const labels=():Record<string,string>=>({queued:t("hq.turnStatus.queued"),running:t("hq.turnStatus.running"),finished:t("hq.turnStatus.finished"),failed:t("hq.turnStatus.failed"),interrupted:t("hq.turnStatus.interrupted")});
const n=(v?:number)=>new Intl.NumberFormat(intlTag(currentLocale())).format(v||0);
export function HqTurns({data,error}:{data?:TurnTelemetry;error?:string}) {
  const sessions=useWorldStore(s=>s.sessions);
  const agents=sessions.flatMap(s=>s.subagents.map(a=>({...a,project:s.slug||s.project})));
  const turns=(data?.turns||[]).slice().sort((a,b)=>b.queued-a.queued);
  const st=labels();
  return <><h3>{t("hq.turns.heading")}</h3>
    <p className="hq-muted">{t("hq.turns.note")}</p>
    {(error||data?.error)&&<p className="hq-error">{error||data?.error}</p>}
    {data?.buffering.map((b,i)=><p key={i}>{t("hq.turns.buffering",{project:b.project,count:b.messages})}</p>)}
    {turns.slice(0,100).map(turn=><details key={turn.id}><summary><span className={`hq-status ${turn.status==="failed"?"error":turn.status==="running"?"running":""}`}>{st[turn.status]||turn.status}</span> · {turn.project} · {new Date(turn.queued*1000).toLocaleString(intlTag(currentLocale()))} · {t("hq.turns.toolsCount",{count:turn.tools})}</summary><p>{turn.prompt}</p><small>{t("hq.turns.idLabel",{id:turn.id,source:turn.source})}{turn.task_id?t("hq.turns.taskSuffix",{id:turn.task_id}):""}</small><p>{turn.started?t("hq.turns.waitingSec",{sec:Math.max(0,Math.round(turn.started-turn.queued))}):t("hq.turns.waitingStart")}{turn.started&&turn.finished?t("hq.turns.runningSec",{sec:Math.max(0,Math.round(turn.finished-turn.started))}):""} · {turn.tool||t("hq.turns.noTools")}</p><p>{t("hq.turns.usageLine",{input:n(turn.usage.input_tokens),cw:n(turn.usage.cache_creation_input_tokens),cr:n(turn.usage.cache_read_input_tokens),out:n(turn.usage.output_tokens),api:n(turn.requests)})}</p>{turn.reason&&<small>{turn.reason}</small>}</details>)}
    {data&&!turns.length&&<p className="hq-muted">{t("hq.turns.noNewTurns")}</p>}
    <h3>{t("hq.turns.subagentsHeading",{count:agents.length})}</h3><p className="hq-muted">{t("hq.turns.subagentsNote")}</p>
    {agents.map((a,i)=><details key={`${a.project}-${a.agent_id}-${i}`}><summary>{a.project} → {a.description||a.subagent_type||a.agent_id} · {a.status}</summary><p>{a.current.tool_name||a.current.kind} · {a.current.detail}</p><small>{t("hq.turns.parentLabel",{parent:a.parent_agent_id||t("hq.turns.mainWorker"),model:a.model||t("hq.turns.modelUnspecified"),time:a.last_event_at?new Date(a.last_event_at).toLocaleString(intlTag(currentLocale())):t("hq.turns.unknown")})}</small></details>)}
    {!agents.length&&<p className="hq-muted">{t("hq.turns.noSubagents")}</p>}
    <h3>{t("hq.turns.backgroundHeading")}</h3><p className="hq-muted">{t("hq.turns.backgroundNote")}</p>{data?.background.map(bg=><details key={`${bg.project}-${bg.id}`}><summary>{bg.project} · {bg.status} · {bg.id}</summary><p>{bg.description}</p><small>{new Date(bg.last_event*1000).toLocaleString(intlTag(currentLocale()))}</small></details>)}{!data?.background.length&&<p className="hq-muted">{t("hq.turns.noBackgroundEvents")}</p>}
  </>;
}
