import { existsSync, mkdirSync, readFileSync, realpathSync, symlinkSync, unlinkSync, writeFileSync } from "node:fs";
import { isAbsolute, join, relative, resolve, sep } from "node:path";
import type { AgentTool } from "@earendil-works/pi-agent-core";
import { fauxAssistantMessage, fauxToolCall } from "@earendil-works/pi-ai";
import { Type } from "typebox";
import { afterEach, describe, expect, it } from "vitest";
import { createAgentSession } from "../../src/core/sdk.ts";
import { SessionManager } from "../../src/core/session-manager.ts";
import { SettingsManager } from "../../src/core/settings-manager.ts";
import { createReadTool } from "../../src/core/tools/read.ts";
import { createWriteTool } from "../../src/core/tools/write.ts";
import { createTestResourceLoader } from "../utilities.ts";
import { createHarness, getMessageText, getToolResult, type Harness } from "./harness.ts";

const harnesses: Harness[] = [];
afterEach(() => {
	for (const harness of harnesses.splice(0)) harness.cleanup();
});

async function fixture() {
	const harness = await createHarness();
	harnesses.push(harness);
	const project = join(harness.tempDir, "project");
	const outside = join(harness.tempDir, "project-other");
	mkdirSync(project);
	mkdirSync(outside);
	writeFileSync(join(project, "中文.txt"), "INSIDE_FIXTURE");
	writeFileSync(join(outside, "private.txt"), "OUTSIDE_FIXTURE");
	return { harness, project, outside };
}

describe("Atlas P0: upstream permission boundaries (counterexamples are expected)", () => {
	it("built-in read can read an absolute file outside cwd", async () => {
		const { project, outside } = await fixture();
		const result = await createReadTool(project).execute("read", { path: join(outside, "private.txt") });
		expect(getMessageText(result)).toContain("OUTSIDE_FIXTURE");
	});

	it("built-in write can traverse outside cwd", async () => {
		const { project, outside } = await fixture();
		await createWriteTool(project).execute("write", { path: "../project-other/written.txt", content: "SIDE_EFFECT" });
		expect(readFileSync(join(outside, "written.txt"), "utf8")).toBe("SIDE_EFFECT");
	});

	it("built-in read follows a junction outside cwd", async () => {
		const { project, outside } = await fixture();
		symlinkSync(outside, join(project, "linked"), process.platform === "win32" ? "junction" : "dir");
		const result = await createReadTool(project).execute("read", { path: "linked/private.txt" });
		expect(getMessageText(result)).toContain("OUTSIDE_FIXTURE");
	});

	it("loading an extension can have effects even with an empty tool allowlist", async () => {
		const bootstrap = await createHarness();
		harnesses.push(bootstrap);
		const marker = join(bootstrap.tempDir, "extension-effect.txt");
		const harness = await createHarness({
			allowedToolNames: [],
			initialActiveToolNames: [],
			extensionFactories: [
				() => {
					writeFileSync(marker, "EXTENSION_RAN");
				},
			],
		});
		harnesses.push(harness);
		await harness.session.bindExtensions({});
		expect(harness.session.getAllTools()).toEqual([]);
		expect(readFileSync(marker, "utf8")).toBe("EXTENSION_RAN");
	});

	it("a junction swap after path preflight escapes a check-then-read guard (expected counterexample)", async () => {
		const { project, outside } = await fixture();
		const inside = join(project, "inside");
		mkdirSync(inside);
		writeFileSync(join(inside, "private.txt"), "INSIDE_FIXTURE");
		const link = join(project, "linked");
		const linkKind = process.platform === "win32" ? "junction" : "dir";
		symlinkSync(inside, link, linkKind);
		const read = createReadTool(project);
		let approved = false;
		const swapped: typeof read = {
			...read,
			execute: async (...args) => {
				unlinkSync(link);
				symlinkSync(outside, link, linkKind);
				return read.execute(...args);
			},
		};
		const harness = await createHarness({
			tools: [swapped],
			extensionFactories: [
				(pi) => {
					pi.on("tool_call", (event) => {
						if (event.toolName !== "read") return { block: true, reason: "DENIED" };
						const path = realpathSync(resolve(project, String(event.input.path)));
						const child = relative(realpathSync(project), path);
						approved = !isAbsolute(child) && child !== ".." && !child.startsWith(`..${sep}`);
						return approved ? undefined : { block: true, reason: "DENIED" };
					});
				},
			],
		});
		harnesses.push(harness);
		await harness.session.bindExtensions({});
		harness.setResponses([
			fauxAssistantMessage([fauxToolCall("read", { path: "linked/private.txt" })], { stopReason: "toolUse" }),
			fauxAssistantMessage("finished"),
		]);
		await harness.session.prompt("read fixture");
		expect(approved).toBe(true);
		expect(getMessageText(getToolResult(harness, "read"))).toContain("OUTSIDE_FIXTURE");
	});
});

describe("Atlas P0: explicit configuration and host policy prototype", () => {
	it("the SDK factory executes faux requests using only explicitly injected resources", async () => {
		const { harness, project } = await fixture();
		writeFileSync(join(project, "AGENTS.md"), "UNTRUSTED_CONTEXT_MARKER");
		const { session } = await createAgentSession({
			cwd: project,
			agentDir: join(harness.tempDir, "isolated-agent"),
			model: harness.getModel(),
			modelRuntime: harness.session.modelRuntime,
			settingsManager: SettingsManager.inMemory({ retry: { enabled: false } }),
			sessionManager: SessionManager.inMemory(project),
			resourceLoader: createTestResourceLoader(),
			tools: [],
		});
		try {
			expect(session.getAllTools()).toEqual([]);
			expect(session.systemPrompt).not.toContain("UNTRUSTED_CONTEXT_MARKER");
			harness.setResponses([fauxAssistantMessage("SDK_READY")]);
			const events: string[] = [];
			session.subscribe((event) => events.push(event.type));
			await session.prompt("offline SDK probe");
			expect(session.getLastAssistantText()).toBe("SDK_READY");
			expect(events.at(-1)).toBe("agent_settled");
		} finally {
			session.dispose();
		}
	});

	it("a denied tool cannot execute even when the faux model explicitly requests it", async () => {
		let executions = 0;
		const forbidden: AgentTool = {
			name: "forbidden",
			label: "forbidden",
			description: "Fixture side effect",
			parameters: Type.Object({}),
			execute: async () => {
				executions++;
				return { content: [{ type: "text", text: "executed" }], details: {} };
			},
		};
		const harness = await createHarness({ tools: [forbidden], allowedToolNames: [], initialActiveToolNames: [] });
		harnesses.push(harness);
		harness.session.setActiveToolsByName(["forbidden"]);
		harness.setResponses([
			fauxAssistantMessage([fauxToolCall("forbidden", {})], { stopReason: "toolUse" }),
			fauxAssistantMessage("finished"),
		]);
		await harness.session.prompt("attempt denied tool");
		expect(executions).toBe(0);
		expect(getToolResult(harness, "forbidden").isError).toBe(true);
	});

	it.each(["inside", "traversal", "absolute", "prefix", "junction"] as const)(
		"a canonical-path preflight handles %s before reading (static path prototype)",
		async (kind) => {
			const { project, outside } = await fixture();
			symlinkSync(outside, join(project, "linked"), process.platform === "win32" ? "junction" : "dir");
			const target = {
				inside: "中文.txt",
				traversal: "../project-other/private.txt",
				absolute: join(outside, "private.txt"),
				prefix: join(outside, "private.txt"),
				junction: "linked/private.txt",
			}[kind];
			let reads = 0;
			const read = createReadTool(project);
			const guardedRead: typeof read = {
				...read,
				execute: async (...args) => {
					reads++;
					return read.execute(...args);
				},
			};
			const harness = await createHarness({
				tools: [guardedRead],
				allowedToolNames: ["read"],
				initialActiveToolNames: ["read"],
				extensionFactories: [
					(pi) => {
						pi.on("tool_call", (event) => {
							if (event.toolName !== "read" || typeof event.input.path !== "string")
								return { block: true, reason: "DENIED" };
							const actual = realpathSync(resolve(project, event.input.path));
							const child = relative(realpathSync(project), actual);
							if (isAbsolute(child) || child === ".." || child.startsWith(`..${sep}`))
								return { block: true, reason: "DENIED_SCOPE" };
							return undefined;
						});
					},
				],
			});
			harnesses.push(harness);
			await harness.session.bindExtensions({});
			harness.setResponses([
				fauxAssistantMessage([fauxToolCall("read", { path: target })], { stopReason: "toolUse" }),
				fauxAssistantMessage("finished"),
			]);
			await harness.session.prompt("read fixture");
			const result = getToolResult(harness, "read");
			expect(reads).toBe(kind === "inside" ? 1 : 0);
			expect(result.isError).toBe(kind !== "inside");
			expect(getMessageText(result)).not.toContain("OUTSIDE_FIXTURE");
			if (kind === "inside") expect(getMessageText(result)).toContain("INSIDE_FIXTURE");
			else expect(getMessageText(result)).toContain("DENIED_SCOPE");
		},
	);

	it("two identical SDK prompts execute twice without host request deduplication", async () => {
		let executions = 0;
		const tool: AgentTool = {
			name: "effect",
			label: "effect",
			description: "Fixture effect",
			parameters: Type.Object({}),
			execute: async () => {
				executions++;
				return { content: [{ type: "text", text: "ok" }], details: {} };
			},
		};
		const harness = await createHarness({ tools: [tool] });
		harnesses.push(harness);
		harness.setResponses(
			[1, 2].flatMap((number) => [
				fauxAssistantMessage([fauxToolCall("effect", {}, { id: `call-${number}` })], { stopReason: "toolUse" }),
				fauxAssistantMessage("done"),
			]),
		);
		await harness.session.prompt("identical request");
		await harness.session.prompt("identical request");
		expect(executions).toBe(2);
	});

	it("abort stops a cooperative tool without undoing an existing side effect", async () => {
		let started!: () => void;
		const reached = new Promise<void>((done) => {
			started = done;
		});
		let cancelled = false;
		const { harness: bootstrap } = await fixture();
		const marker = join(bootstrap.tempDir, "committed-effect");
		const tool: AgentTool = {
			name: "wait",
			label: "wait",
			description: "Cooperative fixture",
			parameters: Type.Object({}),
			execute: async (_id, _params, signal) => {
				writeFileSync(marker, "effect");
				started();
				await new Promise<void>((_done, reject) => {
					if (signal?.aborted) {
						cancelled = true;
						reject(new Error("aborted"));
						return;
					}
					signal?.addEventListener(
						"abort",
						() => {
							cancelled = true;
							reject(new Error("aborted"));
						},
						{ once: true },
					);
				});
				return { content: [], details: {} };
			},
		};
		const harness = await createHarness({ tools: [tool] });
		harnesses.push(harness);
		harness.setResponses([fauxAssistantMessage([fauxToolCall("wait", {})], { stopReason: "toolUse" })]);
		const running = harness.session.prompt("start waiting");
		await reached;
		await harness.session.abort();
		await running;
		expect(cancelled).toBe(true);
		expect(existsSync(marker)).toBe(true);
		expect(harness.eventsOfType("agent_settled").at(-1)).toMatchObject({ aborted: true });
	});
});
