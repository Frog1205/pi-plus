import { spawn } from "node:child_process";
import { appendFileSync, mkdirSync, mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

export interface ProbeResult {
	type: string;
	status?: string;
	effects?: number;
	attempts?: number;
	ids?: number[];
	collisionId?: number;
	toolErrors?: number;
	lateChildEffect?: boolean;
	userEntries?: number;
	assistantCalls?: number;
	toolResults?: number;
	isStreaming?: boolean;
}

export function directory(): string {
	const path = mkdtempSync(join(tmpdir(), "atlas-p0-crash-"));
	mkdirSync(join(path, "project"));
	return path;
}

export async function probe(
	engine: string,
	path: string,
	phase: string,
	replay = "unsafe",
	kill = false,
): Promise<ProbeResult> {
	const worker = fileURLToPath(new URL("./fixtures/atlas-p0-worker.ts", import.meta.url));
	const resolver = new URL("../../coding-agent/src/experimental/source-resolver.ts", import.meta.url).href;
	const child = spawn(process.execPath, ["--import", resolver, worker, engine, path, phase, replay], {
		env: process.env,
		stdio: ["ignore", "ignore", "pipe", "ipc"],
		windowsHide: true,
	});
	let stderr = "";
	child.stderr!.on("data", (chunk) => {
		stderr += chunk.toString();
	});
	return new Promise((done, fail) => {
		let result: ProbeResult | undefined;
		let killed = false;
		const timer = setTimeout(() => {
			child.kill("SIGKILL");
			fail(new Error(`P0 child timeout ${phase}: ${stderr}`));
		}, 25000);
		child.on("message", (message) => {
			const record = message as ProbeResult;
			if (record.type === "barrier" && kill) {
				killed = true;
				child.kill("SIGKILL");
			}
			if (record.type === "result") result = record;
		});
		child.once("error", (error) => {
			clearTimeout(timer);
			fail(error);
		});
		child.once("close", (code) => {
			clearTimeout(timer);
			if (process.env.ATLAS_P0_EVIDENCE) {
				appendFileSync(
					process.env.ATLAS_P0_EVIDENCE,
					`${JSON.stringify({ engine, path, phase, replay, killed, code, result })}\n`,
				);
			}
			if (kill && killed) done({ type: "killed" });
			else if (code === 0 && result) done(result);
			else fail(new Error(`P0 child exited ${code} (${engine}/${phase}): ${stderr}`));
		});
	});
}
