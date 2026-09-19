import { describe,expect,it } from "vitest";
import {workerSignal} from "./HqOperations";
import type { Worker } from "./HqOffice";
const now=Date.parse("2026-09-17T12:00:00Z");
const worker:Worker={alive:true,busy:false,turns:1,cwd:"/tmp",session:"a",usage:{context:0,requests:0,prompt:"",last_event:"2026-09-17T10:00:00Z",totals:{},last:{}}};
describe("operator signals",()=>{
  it("does not flag an idle worker for an old event",()=>expect(workerSignal(worker,now,false).title).toBe("Free"));
  it("does not call a long tool a confirmed hang",()=>{const s=workerSignal({...worker,busy:true},now,false);expect(s.tone).toBe("warn");expect(s.detail).toContain("isn't confirmed");});
  it("marks offline data stale instead of declaring a worker stopped",()=>expect(workerSignal({...worker,alive:false},now,true).title).toBe("Data is stale"));
  it("handles missing usage",()=>expect(workerSignal({...worker,busy:true,usage:undefined},now,false).title).toBe("Working · no log"));
  it("shows fresh busy worker and stopped worker",()=>{expect(workerSignal({...worker,busy:true},now-119*60000,false).title).toBe("Working");expect(workerSignal({...worker,alive:false},now,false).title).toBe("Unavailable");});
});
