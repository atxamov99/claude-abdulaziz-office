import { describe, expect, it } from "vitest";
import { aggregateDays, type History } from "./HqHistory";

describe("HQ daily history",()=>{
  const history:History={since:"",events:[],tasks:{},sessions:{
    a:{project:"hq",days:{"2026-09-17":{output_tokens:10,requests:1},"2026-09-16":{output_tokens:5,requests:1}}},
    b:{project:"hq",days:{"2026-09-17":{output_tokens:20,requests:2}}},
    c:{project:"mobile",days:{"2026-09-17":{output_tokens:30,requests:3}}},
  }};
  it("sums distinct session snapshots, not repeated observations",()=>{
    expect(aggregateDays(history,"")[0]).toEqual(["2026-09-17",{output_tokens:60,requests:6}]);
    expect(aggregateDays(history,"")[0]).toEqual(["2026-09-17",{output_tokens:60,requests:6}]);
  });
  it("filters projects and sorts dates newest first",()=>{
    expect(aggregateDays(history,"hq")).toEqual([["2026-09-17",{output_tokens:30,requests:3}],["2026-09-16",{output_tokens:5,requests:1}]]);
    expect(aggregateDays(history,"missing")).toEqual([]);
  });
});
