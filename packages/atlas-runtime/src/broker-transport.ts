import { createHash, randomUUID } from "node:crypto";
import type { JsonValue } from "@earendil-works/pi-ai";

export interface ProbeConfiguration {
	protocol: 2;
	mode: "model_fixture";
	run_id: string;
	request_id: string;
	goal: string;
}
interface ModelReply {
	resolve: (response: Response) => void;
	reject: (error: Error) => void;
	chunks: Buffer[];
	sequence: number;
	size: number;
	started: boolean;
	status: number;
	cleanup: () => void;
}

/** Fixture-only IPC transport. It never opens a network connection. */
export class BrokerTransport {
	private buffer = Buffer.alloc(0);
	private readonly models = new Map<string, ModelReply>();
	private readonly tools = new Map<string, { resolve: (value: JsonValue) => void; reject: (error: Error) => void }>();
	private readonly cancelled = new Set<string>();
	private readonly configure: (value: ProbeConfiguration) => void;
	readonly configuration: Promise<ProbeConfiguration>;
	readonly abort = new AbortController();
	fetchCalls = 0;
	fatal: string | undefined;

	constructor() {
		let configure!: (value: ProbeConfiguration) => void;
		this.configuration = new Promise((resolve) => {
			configure = resolve;
		});
		this.configure = configure;
		process.stdin.on("data", (chunk: Buffer) => {
			try {
				this.buffer = Buffer.concat([this.buffer, chunk]);
				if (this.buffer.length > 1_048_576) throw new Error("protocol_record_limit");
				for (let index = this.buffer.indexOf(10); index >= 0; index = this.buffer.indexOf(10)) {
					const line = this.buffer.subarray(0, index).toString("utf8");
					this.buffer = this.buffer.subarray(index + 1);
					this.receive(JSON.parse(line) as Record<string, JsonValue>);
				}
			} catch {
				this.fail("invalid_broker_protocol");
			}
		});
		process.stdin.on("end", () => this.fail("host_disconnected"));
	}

	send(value: object): void {
		process.stdout.write(`${JSON.stringify({ protocol: 2, ...value })}\n`);
	}

	private fail(code: string): void {
		this.fatal ??= code;
		for (const pending of this.models.values()) {
			pending.cleanup();
			pending.reject(new Error(code));
		}
		for (const pending of this.tools.values()) pending.reject(new Error(code));
		this.models.clear();
		this.tools.clear();
	}

	private receive(message: Record<string, JsonValue>): void {
		if (message.protocol !== 2) throw new Error("protocol_version");
		if (message.type === "start") {
			if (
				message.mode !== "model_fixture" ||
				typeof message.run_id !== "string" ||
				typeof message.goal !== "string" ||
				typeof message.request_id !== "string"
			)
				throw new Error("configuration");
			this.configure(message as unknown as ProbeConfiguration);
			return;
		}
		if (message.type === "cancel") {
			this.abort.abort();
			return;
		}
		if (typeof message.id !== "string") throw new Error("reply_id");
		if (this.cancelled.has(message.id)) return;
		if (message.type === "tool_result") {
			const pending = this.tools.get(message.id);
			if (!pending) throw new Error("unknown_tool_reply");
			this.tools.delete(message.id);
			if (message.ok === true) pending.resolve(message.result ?? null);
			else pending.reject(new Error(String(message.error)));
			return;
		}
		const pending = this.models.get(message.id);
		if (!pending) throw new Error("unknown_model_reply");
		if (message.type === "model_start") {
			if (
				pending.started ||
				typeof message.status !== "number" ||
				!Number.isInteger(message.status) ||
				message.status < 200 ||
				message.status > 599
			)
				throw new Error("model_start");
			pending.status = message.status;
			pending.started = true;
		} else if (message.type === "model_chunk") {
			if (
				!pending.started ||
				message.sequence !== pending.sequence ||
				typeof message.data !== "string" ||
				message.data.length > 65536 ||
				!/^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/.test(message.data)
			)
				throw new Error("model_chunk");
			const chunk = Buffer.from(message.data, "base64");
			pending.size += chunk.length;
			if (pending.size > 262144) throw new Error("model_body_limit");
			pending.sequence++;
			pending.chunks.push(chunk);
		} else if (message.type === "model_end") {
			const body = Buffer.concat(pending.chunks);
			if (
				!pending.started ||
				message.size !== body.length ||
				message.sha256 !== createHash("sha256").update(body).digest("hex")
			)
				throw new Error("model_checksum");
			this.models.delete(message.id);
			pending.cleanup();
			pending.resolve(
				new Response(body, {
					status: pending.status,
					headers: { "content-type": pending.status === 200 ? "text/event-stream" : "application/json" },
				}),
			);
		} else throw new Error("model_reply_type");
	}

	async fetch(input: string | URL | Request, init: RequestInit | undefined, generationId: string): Promise<Response> {
		if (this.fatal) throw new Error(this.fatal);
		if (
			String(input) !== "https://atlas-fixture.invalid/v1/responses" ||
			init?.method !== "POST" ||
			typeof init.body !== "string" ||
			!generationId
		)
			throw new Error("transport_request_denied");
		const signal = init.signal ? AbortSignal.any([init.signal, this.abort.signal]) : this.abort.signal;
		signal.throwIfAborted();
		const id = randomUUID();
		this.fetchCalls++;
		return new Promise((resolve, reject) => {
			const cancel = () => {
				this.models.delete(id);
				this.cancelled.add(id);
				this.send({ type: "model_abort", id });
				reject(new DOMException("Aborted", "AbortError"));
			};
			signal.addEventListener("abort", cancel, { once: true });
			this.models.set(id, {
				resolve,
				reject,
				chunks: [],
				sequence: 0,
				size: 0,
				status: 0,
				started: false,
				cleanup: () => signal.removeEventListener("abort", cancel),
			});
			this.send({ type: "model_request", id, generation_id: generationId, body: init.body });
		});
	}

	tool(name: string, parameters: object): Promise<JsonValue> {
		const id = randomUUID();
		return new Promise((resolve, reject) => {
			this.tools.set(id, { resolve, reject });
			this.send({ type: "tool_request", id, tool: name, parameters });
		});
	}
}
