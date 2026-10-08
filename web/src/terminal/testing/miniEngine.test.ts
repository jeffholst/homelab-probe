import { describe, expect, it } from "vitest";

import { MOCK_CAPABILITIES } from "../mock";
import { miniEngine } from "./miniEngine";

const run = (actions: Parameters<typeof miniEngine.step>[1][]) =>
  actions.reduce((state, action) => miniEngine.step(state, action).state, miniEngine.step(miniEngine.initialState(), { type: "setCapabilities", capabilities: MOCK_CAPABILITIES }).state);

describe("the stand-in engine", () => {
  it("locks the prompt while a command runs: typing, paste, history and Tab change nothing", () => {
    let state = run([{ type: "insert", text: "info" }]);
    const submitted = miniEngine.step(state, { type: "submit" });
    state = submitted.state;
    expect(state.running).toBe(true);
    const seq = state.activeExecution ?? 0;
    for (const action of [{ type: "insert", text: "x" }, { type: "paste", text: "y" }, { type: "backspace" }, { type: "historyPrevious" }, { type: "tab" }, { type: "clearLine" }] as const) {
      expect(miniEngine.step(state, action).state.line).toEqual({ text: "", cursor: 0 });
    }
    expect(miniEngine.step(state, { type: "interrupt" }).effects).toEqual([{ type: "interrupted", running: true }]);
    const finished = miniEngine.step(state, { type: "executionFinished", seq }).state;
    expect(finished.running).toBe(false);
    expect(finished.line.text).toBe("");
    expect(miniEngine.step(finished, { type: "insert", text: "w" }).state.line.text).toBe("w");
  });
});
