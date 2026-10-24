import { join } from "node:path";
import { BACKGROUND_CONTEXT } from "@earendil-works/chord/context";
import { type Api, createModels, createProvider, type Model, Type } from "@earendil-works/pi-ai";
import { stream, streamSimple } from "@earendil-works/pi-ai/api/openai-responses";
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
import { BrokerTransport } from "../src/broker-transport.ts";

const transport = new BrokerTransport();
const config = await transport.configuration;
// A transport regression must fail closed instead of reaching any real endpoint.
let nativeFetchCalls = 0;
globalThis.fetch = async () => {
	nativeFetchCalls++;
	throw new Error("native_network_forbidden");
};
const models = createModels();
let generationId = "";
let denied = false;
function responseModel(model: Model<Api>): Model<"openai-responses"> {
	if (model.api !== "openai-responses") throw new Error("fixture_dialect");
	return model as Model<"openai-responses">;
}
models.setProvider(
	createProvider({
		id: "atlas-fixture",
		baseUrl: "https://atlas-fixture.invalid/v1",
		auth: {
			apiKey: {
				name: "Fixture placeholder",
				resolve: async () => ({ auth: { apiKey: "not-a-provider-credential" } }),
			},
		},
		models: [
			{
				id: "atlas-fixture-model",
				name: "Fixture Model",
				provider: "atlas-fixture",
				baseUrl: "https://atlas-fixture.invalid/v1",
				api: "openai-responses",
				reasoning: false,
				input: ["text"],
				cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
				contextWindow: 32768,
				maxTokens: 1024,
			},
		],
		api: {
			stream: (model, context, options) =>
				stream(responseModel(model), context, {
					...options,
					maxRetries: 0,
					cacheRetention: "none",
					maxTokens: 1024,
					fetch: (input, init) => transport.fetch(input, init, generationId),
				}),
			streamSimple: (model, context, options) =>
				streamSimple(responseModel(model), context, {
					...options,
					maxRetries: 0,
					cacheRetention: "none",
					maxTokens: 1024,
					fetch: (input, init) => transport.fetch(input, init, generationId),
				}),
		},
	}),
);
const registry = createRegistry();
registry.install(
	defineExtension({
		name: "atlas-model-probe-v1",
		hooks: [
			hook(GenerationTask, {
				beforeRequest: async (_request, api, context) => {
					const identity = await api.memo("atlas-probe-request", api.taskId, context);
					if (typeof identity !== "number") throw new Error("generation_identity");
					generationId = String(identity);
					transport.send({ type: "generation", generation_id: generationId, task_id: api.taskId });
				},
				afterResponse: (message) => {
					if (message.stopReason !== "stop" && message.stopReason !== "toolUse")
						throw new Error("model_response_not_complete");
				},
			}),
			hook(ToolTask, {
				beforeTool: (call) => {
					if (!["read_document", "publish_report"].includes(call.name)) {
						denied = true;
						return { block: "tool_not_granted" };
					}
					return undefined;
				},
			}),
		],
		tools: [
			defineTool({
				name: "read_document",
				description: "Read snapshot d1",
				replay: "safe",
				parameters: Type.Object({ document_id: Type.String() }, { additionalProperties: false }),
				execute: async (args) => ({
					output: [{ type: "text", text: JSON.stringify(await transport.tool("read_document", args)) }],
				}),
			}),
			defineTool({
				name: "publish_report",
				description: "Publish one receipt-backed report",
				replay: "safe",
				parameters: Type.Object({ content: Type.String() }, { additionalProperties: false }),
				execute: async (args) => ({
					output: [{ type: "text", text: JSON.stringify(await transport.tool("publish_report", args)) }],
				}),
			}),
		],
	}),
);
const context = BACKGROUND_CONTEXT;
const harness = await Harness.open(
	await openNodeSqliteStorage(join(process.cwd(), "durable.sqlite")),
	{
		models,
		registry,
		settings: { retry: { enabled: false, maxRetries: 0 }, stream: { maxRetries: 0 }, compaction: { enabled: false } },
	},
	context,
);
try {
	const root = await harness.root(context, {
		agent: { model: { provider: "atlas-fixture", modelId: "atlas-fixture-model" } },
	});
	const submission = await root.submit({ type: "input", content: config.goal, requestId: config.request_id }, context);
	const settled = await submission.wait(context);
	transport.send({
		type: "result",
		status: settled.status,
		mode: "model_fixture",
		model_connected: false,
		native_fetch_calls: nativeFetchCalls,
		fetch_calls: transport.fetchCalls,
		denied,
		protocol_error: transport.fatal ?? null,
	});
} catch {
	transport.send({ type: "error", code: "probe_worker_failed" });
} finally {
	await harness.close(context);
	process.stdin.pause();
}
