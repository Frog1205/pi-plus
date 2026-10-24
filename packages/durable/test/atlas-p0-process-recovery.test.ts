import { existsSync } from "node:fs";
import { join } from "node:path";
import { fauxAssistantMessage, fauxToolCall } from "@earendil-works/pi-ai";
import { LiveDoc } from "@earendil-works/pi-durable";
import { describe, expect, it } from "vitest";
import { NodeExecutionEnv } from "../src/env/node.ts";
import { openNodeSqliteStorage } from "../src/storage/sqlite/node.ts";
import { createBashTool } from "../src/tools/index.ts";
import { directory, probe } from "./atlas-p0-process-support.ts";
import { chatSetup, openChat, waitFor } from "./chat-support.ts";
import { addTool } from "./harness-support.ts";
import { context } from "./session-support.ts";

describe("Atlas P0: real child-process termination and SQLite recovery", () => {
	it("a durable beforeTool host policy blocks the side effect", async () => {
		const result = await probe("durable", directory(), "blocked");
		expect(result.effects).toBe(0);
		expect(result.attempts).toBe(0);
		expect(result.toolErrors).toBe(1);
	});

	it("force-killing a Worker does not kill a detached tool subprocess (expected counterexample)", async () => {
		const path = directory();
		await probe("durable", path, "child-process", "unsafe", true);
		await waitFor(() => existsSync(join(path, "late-child-effect.txt")), 10000);
		const reopened = await probe("durable", path, "resume");
		expect(reopened.attempts).toBe(1);
		expect(reopened.toolErrors).toBe(1);
		expect(reopened.lateChildEffect).toBe(true);
	}, 60000);

	it("closing a harness kills the running Bash tree before its delayed file effect", async () => {
		const path = directory();
		const env = new NodeExecutionEnv({ cwd: path, shellPath: process.env.ATLAS_P0_BASH });
		const setup = chatSetup();
		addTool(setup.registry, createBashTool());
		setup.faux.setResponses([
			fauxAssistantMessage(
				[fauxToolCall("bash", { command: "echo started; sleep 3; printf late > late.txt", timeout: 10 })],
				{ stopReason: "toolUse" },
			),
		]);
		const opened = await openChat(await openNodeSqliteStorage(join(path, "shell.sqlite")), setup, { env });
		try {
			await opened.root.submit({ type: "input", content: "run temporary shell fixture" }, context);
			await waitFor(
				async () =>
					(await opened.harness.snapshot(LiveDoc, opened.root.id, context))?.tools?.[0]?.output?.includes(
						"started",
					) === true,
				10000,
			);
		} finally {
			await opened.harness.close(context);
		}
		await new Promise((done) => setTimeout(done, 3500));
		expect(existsSync(join(path, "late.txt"))).toBe(false);
	}, 20000);

	it.each(["mid-generation", "before-effect"])(
		"recovers %s without blindly replaying an unsafe effect",
		async (phase) => {
			const path = directory();
			expect(await probe("durable", path, phase, "unsafe", true)).toEqual({ type: "killed" });
			const result = await probe("durable", path, "resume");
			expect(result.status).toBe("done");
			expect(result.userEntries).toBe(1);
			expect(new Set(result.ids).size).toBe(1);
			if (phase === "before-effect") {
				expect(result.effects).toBe(0);
				expect(result.toolErrors).toBe(1);
			} else {
				expect(result.effects).toBe(1);
				expect(result.toolErrors).toBe(0);
			}
		},
		60000,
	);

	it("unsafe: after side effect but before result commit, does not repeat and reports uncertainty", async () => {
		const path = directory();
		await probe("durable", path, "after-effect", "unsafe", true);
		const result = await probe("durable", path, "resume");
		expect(result.effects).toBe(1);
		expect(result.attempts).toBe(1);
		expect(result.toolErrors).toBe(1);
		// The harness can finish despite a tool error. Atlas must not infer business success from this alone.
		expect(result.status).toBe("done");
	}, 60000);

	it("safe label on a non-idempotent tool causes a duplicate after a crash (expected counterexample)", async () => {
		const path = directory();
		await probe("durable", path, "after-effect", "safe", true);
		const result = await probe("durable", path, "resume", "safe");
		expect(result.effects).toBe(2);
		expect(result.attempts).toBe(2);
		expect(result.toolErrors).toBe(0);
	}, 60000);

	it("an exclusive-create idempotent fixture tolerates safe replay with one effect", async () => {
		const path = directory();
		await probe("durable", path, "after-effect", "idempotent", true);
		const result = await probe("durable", path, "resume", "idempotent");
		expect(result.effects).toBe(1);
		expect(result.attempts).toBe(2);
		expect(result.status).toBe("done");
	}, 60000);

	it("after the tool result is committed, restart does not repeat the completed effect", async () => {
		const path = directory();
		await probe("durable", path, "after-result", "unsafe", true);
		const result = await probe("durable", path, "resume");
		expect(result.effects).toBe(1);
		expect(result.attempts).toBe(1);
		expect(result.toolErrors).toBe(0);
		expect(result.status).toBe("done");
	}, 60000);

	it("request IDs deduplicate concurrent submissions and reopen, but do not check payload equality", async () => {
		const path = directory();
		const first = await probe("durable", path, "complete");
		const reopened = await probe("durable", path, "resume");
		expect(new Set(first.ids).size).toBe(1);
		expect(reopened.ids).toEqual(first.ids);
		expect(first.collisionId).toBe(first.ids?.[0]);
		expect(reopened.userEntries).toBe(1);
		expect(reopened.effects).toBe(1);
	}, 60000);
});
