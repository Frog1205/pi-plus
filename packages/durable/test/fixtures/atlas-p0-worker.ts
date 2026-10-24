import { spawn } from "node:child_process";
import { appendFileSync, existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { BACKGROUND_CONTEXT } from "@earendil-works/chord/context";
import { createModels, fauxAssistantMessage, fauxProvider, fauxToolCall, Type } from "@earendil-works/pi-ai";
import {
	createRegistry,
	defineExtension,
	defineTool,
	Harness,
	hook,
	ToolResultEntry,
	ToolTask,
} from "@earendil-works/pi-durable";
import { createAgentSession } from "../../../coding-agent/src/core/sdk.ts";
import { SessionManager } from "../../../coding-agent/src/core/session-manager.ts";
import { SettingsManager } from "../../../coding-agent/src/core/settings-manager.ts";
import { createHarness } from "../../../coding-agent/test/suite/harness.ts";
import { createTestResourceLoader } from "../../../coding-agent/test/utilities.ts";
import { openNodeSqliteStorage } from "../../src/storage/sqlite/node.ts";

const [engine, rootPath, phase, replay = "unsafe"] = process.argv.slice(2);
if (!rootPath || !phase || !["sdk", "durable"].includes(engine)) throw new Error("Invalid P0 worker arguments");
mkdirSync(rootPath, { recursive: true });
const context = BACKGROUND_CONTEXT;
const effectPath = join(rootPath, "effects.txt");
const attemptPath = join(rootPath, "attempts.txt");

function count(path: string): number {
	return existsSync(path) ? readFileSync(path, "utf8").trim().split("\n").filter(Boolean).length : 0;
}

function send(message: object): void {
	if (!process.send) throw new Error("P0 worker requires IPC");
	process.send(message);
}

async function pause(barrier: string): Promise<never> {
	send({ type: "barrier", barrier });
	setInterval(() => {}, 1000);
	return new Promise<never>(() => {});
}

async function effect(): Promise<void> {
	appendFileSync(attemptPath, "attempt\n");
	if (phase === "child-process") {
		const entry = fileURLToPath(new URL("./atlas-p0-delayed-effect.ts", import.meta.url));
		const child = spawn(process.execPath, [entry, rootPath], {
			detached: true,
			stdio: "ignore",
			windowsHide: true,
		});
		child.unref();
		await new Promise<void>((done, fail) => {
			const deadline = setTimeout(() => {
				clearInterval(poll);
				fail(new Error("Fixture child did not start"));
			}, 10000);
			const poll = setInterval(() => {
				if (existsSync(join(rootPath, "child-started.txt"))) {
					clearTimeout(deadline);
					clearInterval(poll);
					done();
				}
			}, 10);
			child.once("error", (error) => {
				clearTimeout(deadline);
				clearInterval(poll);
				fail(error);
			});
		});
		await pause("child-process");
	}
	if (phase === "before-effect") await pause("before-effect");
	if (replay === "idempotent") {
		try {
			writeFileSync(effectPath, "effect\n", { flag: "wx" });
		} catch (error) {
			if (!(error instanceof Error && "code" in error && error.code === "EEXIST")) throw error;
		}
	} else appendFileSync(effectPath, "effect\n");
	if (phase === "after-effect") await pause("after-effect");
}

if (engine === "durable") {
	const faux = fauxProvider();
	const models = createModels();
	models.setProvider(faux.provider);
	const registry = createRegistry();
	registry.install(
		defineExtension({
			name: "p0-fixture",
			hooks: phase === "blocked" ? [hook(ToolTask, { beforeTool: () => ({ block: "DENIED_BY_HOST" }) })] : [],
			tools: [
				defineTool({
					name: "effect",
					description: "Temporary fixture effect",
					parameters: Type.Object({}),
					replay: replay === "unsafe" ? "unsafe" : "safe",
					execute: async () => {
						await effect();
						return { output: [{ type: "text", text: "effect complete" }] };
					},
				}),
			],
		}),
	);
	faux.setResponses(
		Array.from({ length: 10 }, () => async (transcript) => {
			if (phase === "mid-generation") return pause("mid-generation");
			if (transcript.messages.some((message) => message.role === "toolResult")) {
				if (phase === "after-result") return pause("after-result");
				return fauxAssistantMessage("done");
			}
			return fauxAssistantMessage([fauxToolCall("effect", {}, { id: "stable-effect-call" })], {
				stopReason: "toolUse",
			});
		}),
	);
	const harness = await Harness.open(
		await openNodeSqliteStorage(join(rootPath, "session.sqlite")),
		{ models, registry },
		context,
	);
	try {
		const root = await harness.root(context, { agent: { model: { provider: "faux", modelId: "faux-1" } } });
		const input = { type: "input", content: "perform fixture effect", requestId: "stable-request" } as const;
		const submissions = await Promise.all([
			root.submit(input, context),
			root.submit(input, context),
			root.submit(input, context),
		]);
		const settled = await submissions[0]!.wait(context);
		const collision = await root.submit({ ...input, content: "DIFFERENT_PAYLOAD" }, context);
		const entries = await root.entries({}, 100, undefined, context);
		const results = entries.items.filter((entry) => ToolResultEntry.is(entry));
		send({
			type: "result",
			engine,
			status: settled.status,
			ids: submissions.map((submission) => submission.id),
			collisionId: collision.id,
			effects: count(effectPath),
			lateChildEffect: existsSync(join(rootPath, "late-child-effect.txt")),
			attempts: count(attemptPath),
			toolErrors: results
				.flatMap((entry) => entry.model ?? [])
				.filter((message) => message.role === "toolResult" && message.isError).length,
			userEntries: entries.items.filter((entry) => entry.kind === "pi.user").length,
		});
	} finally {
		await harness.close(context);
	}
} else {
	const sessionPath = join(rootPath, "session-path.txt");
	const manager = existsSync(sessionPath)
		? SessionManager.open(readFileSync(sessionPath, "utf8"))
		: SessionManager.create(rootPath, join(rootPath, "sessions"));
	const bootstrap = await createHarness();
	const { session } = await createAgentSession({
		cwd: rootPath,
		agentDir: join(rootPath, "agent"),
		model: bootstrap.getModel(),
		modelRuntime: bootstrap.session.modelRuntime,
		sessionManager: manager,
		settingsManager: SettingsManager.inMemory({ retry: { enabled: false } }),
		resourceLoader: createTestResourceLoader(),
		tools: ["effect"],
		customTools: [
			{
				name: "effect",
				label: "effect",
				description: "Temporary fixture effect",
				parameters: Type.Object({}),
				execute: async () => {
					await effect();
					return { content: [{ type: "text", text: "effect complete" }], details: {} };
				},
			},
		],
	});
	session.subscribe(() => {});
	writeFileSync(sessionPath, manager.getSessionFile()!);
	try {
		if (phase === "inspect") {
			await new Promise((done) => setTimeout(done, 150));
			send({
				type: "result",
				engine,
				effects: count(effectPath),
				attempts: count(attemptPath),
				assistantCalls: session.messages.filter(
					(message) => message.role === "assistant" && message.content.some((part) => part.type === "toolCall"),
				).length,
				toolResults: session.messages.filter((message) => message.role === "toolResult").length,
				isStreaming: session.isStreaming,
			});
		} else {
			bootstrap.setResponses([
				fauxAssistantMessage("persisted warmup"),
				fauxAssistantMessage([fauxToolCall("effect", {}, { id: "stable-effect-call" })], { stopReason: "toolUse" }),
				async () => {
					if (phase === "after-result") return pause("after-result");
					return fauxAssistantMessage("done");
				},
			]);
			await session.prompt("warmup for persisted history");
			await session.prompt("perform fixture effect");
			send({ type: "result", engine, effects: count(effectPath), attempts: count(attemptPath) });
		}
	} finally {
		session.dispose();
		bootstrap.cleanup();
	}
}
process.disconnect?.();
