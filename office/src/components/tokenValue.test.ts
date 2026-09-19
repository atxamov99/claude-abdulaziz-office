import {expect,it} from "vitest";
import {tokenValue} from "./tokenValue";
it("handles a fresh session with null usage",()=>expect(tokenValue(null,"input_tokens")).toBe("No data"));
it("handles a missing session",()=>expect(tokenValue(undefined,"input_tokens")).toBe("No data"));
it("distinguishes measured zero from missing data",()=>expect(tokenValue({input_tokens:0},"input_tokens")).toBe("0"));
