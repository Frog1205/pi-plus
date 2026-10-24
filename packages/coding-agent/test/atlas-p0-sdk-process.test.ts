import { describe, expect, it } from "vitest";
import { directory, probe } from "../../durable/test/atlas-p0-process-support.ts";

describe("Atlas P0: SDK persistence across real process termination", () => {
	it.each(["before-effect", "after-effect", "after-result"])(
		"reopens history after %s without automatically recovering the task",
		async (phase) => {
			const path = directory();
			expect(await probe("sdk", path, phase, "unsafe", true)).toEqual({ type: "killed" });
			const result = await probe("sdk", path, "inspect");
			expect(result.effects).toBe(phase === "before-effect" ? 0 : 1);
			expect(result.attempts).toBe(1);
			expect(result.assistantCalls).toBe(1);
			expect(result.toolResults).toBe(phase === "after-result" ? 1 : 0);
			expect(result.isStreaming).toBe(false);
		},
		60000,
	);
});
