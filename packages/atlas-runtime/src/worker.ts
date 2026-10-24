import { randomUUID } from "node:crypto";
import { join } from "node:path";
import { BACKGROUND_CONTEXT } from "@earendil-works/chord/context";
import {
	createModels,
	fauxAssistantMessage,
	fauxProvider,
	fauxToolCall,
	type JsonValue,
	Type,
} from "@earendil-works/pi-ai";
import {
	createRegistry,
	defineExtension,
	defineTool,
	GenerationTask,
	Harness,
	hook,
	ToolTask,
} from "@earendil-works/pi-durable";
import { openNodeSqliteStorage } from "../../durable/src/storage/sqlite/node.ts";

interface Configuration {
	protocol: 1;
	run_id: string;
	request_id: string;
	goal: string;
	documents: { id: string; name: string; sha256: string }[];
	mode: "controlled_offline";
}
interface Incoming {
	type: string;
	id?: string;
	ok?: boolean;
	result?: JsonValue;
	error?: string;
	configuration?: Configuration;
}
const pending = new Map<string, { resolve: (value: JsonValue) => void; reject: (error: Error) => void }>();
let configure!: (value: Configuration) => void;
const configuration = new Promise<Configuration>((done) => {
	configure = done;
});
let buffer = Buffer.alloc(0);
function send(value: object): void {
	process.stdout.write(`${JSON.stringify({ protocol: 1, ...value })}\n`);
}
process.stdin.on("data", (chunk: Buffer) => {
	buffer = Buffer.concat([buffer, chunk]);
	if (buffer.length > 1_048_576) throw new Error("Protocol input limit exceeded");
	for (let index = buffer.indexOf(10); index >= 0; index = buffer.indexOf(10)) {
		const line = buffer.subarray(0, index).toString("utf8");
		buffer = buffer.subarray(index + 1);
		const message = JSON.parse(line) as Incoming;
		if (message.type === "start" && message.configuration) configure(message.configuration);
		else if (message.type === "tool_result" && message.id) {
			const receiver = pending.get(message.id);
			if (!receiver) throw new Error("Unknown gateway response");
			pending.delete(message.id);
			if (message.ok) receiver.resolve(message.result ?? null);
			else receiver.reject(new Error(message.error ?? "Gateway rejected tool"));
		} else throw new Error("Invalid protocol record");
	}
});
process.stdin.on("end", () => {
	for (const receiver of pending.values()) receiver.reject(new Error("Host disconnected"));
	pending.clear();
});
function gateway(tool: string, parameters: object): Promise<JsonValue> {
	const id = randomUUID();
	return new Promise((resolve, reject) => {
		pending.set(id, { resolve, reject });
		send({ type: "tool_request", id, tool, parameters });
	});
}

const config = await configuration;
if (config.protocol !== 1 || config.mode !== "controlled_offline" || !Array.isArray(config.documents))
	throw new Error("Unsupported Worker configuration");
const context = BACKGROUND_CONTEXT;
const registry = createRegistry();
let turns = 0;
registry.install(
	defineExtension({
		name: "atlas-controlled-v1",
		hooks: [
			hook(GenerationTask, {
				beforeRequest: () => {
					if (++turns > 8) throw new Error("Model turn budget exceeded");
				},
			}),
			hook(ToolTask, {
				beforeTool: (call) =>
					["read_document", "publish_report"].includes(call.name) ? undefined : { block: "Tool not granted" },
			}),
		],
		tools: [
			defineTool({
				name: "read_document",
				description: "Read an immutable project document granted by its opaque ID",
				replay: "safe",
				parameters: Type.Object({ document_id: Type.String() }, { additionalProperties: false }),
				execute: async (args) => ({
					output: [{ type: "text", text: JSON.stringify(await gateway("read_document", args)) }],
				}),
			}),
			defineTool({
				name: "publish_report",
				description: "Publish exactly one task report through the host transaction",
				replay: "safe",
				parameters: Type.Object({ content: Type.String() }, { additionalProperties: false }),
				execute: async (args) => ({
					output: [{ type: "text", text: JSON.stringify(await gateway("publish_report", args)) }],
				}),
			}),
		],
	}),
);
const faux = fauxProvider();
const models = createModels();
models.setProvider(faux.provider);
// An explicit offline workflow exercises real durable scheduling and real broker
// tools. It neither pretends to reason with an LLM nor loads user providers.
faux.setResponses(
	Array.from({ length: 10 }, () => (transcript) => {
		const results = transcript.messages.filter((message) => message.role === "toolResult");
		if (results.some((message) => message.isError))
			return fauxAssistantMessage("A tool failed; the host must decide task status.");
		if (results.some((message) => message.toolName === "publish_report"))
			return fauxAssistantMessage("Controlled report published.");
		const read = results.filter((message) => message.toolName === "read_document");
		if (read.length < config.documents.length) {
			return fauxAssistantMessage(
				config.documents
					.slice(read.length)
					.map((document) =>
						fauxToolCall("read_document", { document_id: document.id }, { id: `read-${document.id}` }),
					),
				{ stopReason: "toolUse" },
			);
		}
		const paragraphs = read.map((message) => {
			const text = message.content
				.filter((part) => part.type === "text")
				.map((part) => part.text)
				.join("");
			const document = JSON.parse(text) as { name: string; sha256: string; content: string };
			return `## ${document.name}\n\n来源 SHA-256：${document.sha256}\n\n${document.content.slice(0, 1200)}\n`;
		});
		const content = `# 单项目资料核验报告\n\n模式：controlled_offline（无真实模型推理）\n\n任务：${config.goal}\n\n${paragraphs.join("\n")}\n## 下一步\n\n- 根据以上来源逐项确认项目行动。\n- 需要推断和建议时，另行接入并验收真实模型。\n`;
		return fauxAssistantMessage([fauxToolCall("publish_report", { content }, { id: "publish-report" })], {
			stopReason: "toolUse",
		});
	}),
);
const harness = await Harness.open(
	await openNodeSqliteStorage(join(process.cwd(), "durable.sqlite")),
	{ models, registry },
	context,
);
try {
	const root = await harness.root(context, { agent: { model: { provider: "faux", modelId: "faux-1" } } });
	const submission = await root.submit({ type: "input", content: config.goal, requestId: config.request_id }, context);
	const settled = await submission.wait(context);
	send({
		type: "result",
		status: settled.status,
		mode: config.mode,
		engine: "pi-durable",
		engine_version: "1.4.0",
		run_id: config.run_id,
	});
} catch {
	send({ type: "error", code: "worker_failed" });
	process.exitCode = 1;
} finally {
	await harness.close(context);
	process.stdin.pause();
}
