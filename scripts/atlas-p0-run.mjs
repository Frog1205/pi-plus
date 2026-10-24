import { spawn, spawnSync } from "node:child_process";
import { existsSync, mkdirSync, mkdtempSync, writeFileSync } from "node:fs";
import { delimiter, dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const repository = fileURLToPath(new URL("..", import.meta.url));
const artifactRoot = join(repository, ".artifacts", "atlas-p0");
mkdirSync(artifactRoot, { recursive: true });
const runRoot = mkdtempSync(join(artifactRoot, "run-"));
const testHome = join(runRoot, "home");
const tempRoot = join(runRoot, "tmp");
for (const directory of [testHome, tempRoot, join(testHome, "appdata"), join(testHome, "localappdata")]) {
	mkdirSync(directory, { recursive: true });
}
const environment = {};
for (const name of ["PATH", "SystemRoot", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "SystemDrive", "ProgramFiles", "ProgramFiles(x86)"]) {
	if (process.env[name]) environment[name] = process.env[name];
}
Object.assign(environment, {
	HOME: testHome,
	USERPROFILE: testHome,
	APPDATA: join(testHome, "appdata"),
	LOCALAPPDATA: join(testHome, "localappdata"),
	TMP: tempRoot,
	TEMP: tempRoot,
	TMPDIR: tempRoot,
	XDG_CONFIG_HOME: join(testHome, ".config"),
	XDG_CACHE_HOME: join(testHome, ".cache"),
	PI_OFFLINE: "1",
	PI_NO_LOCAL_LLM: "1",
	AWS_EC2_METADATA_DISABLED: "true",
	GIT_CONFIG_NOSYSTEM: "1",
	GIT_CONFIG_GLOBAL: join(testHome, "gitconfig"),
	GIT_TERMINAL_PROMPT: "0",
	NO_COLOR: "1",
	TZ: "UTC",
});
writeFileSync(environment.GIT_CONFIG_GLOBAL, "");
// Native Windows can otherwise choose the legacy WSL bash launcher ahead of
// a non-default Git installation. Scope this choice to the isolated tests.
let bashPath;
if (process.platform === "win32") {
	const gitPath = spawnSync("where.exe", ["git.exe"], { encoding: "utf8", windowsHide: true }).stdout?.trim().split(/\r?\n/)[0];
	if (gitPath) {
		bashPath = [resolve(dirname(gitPath), "../bin/bash.exe"), resolve(dirname(gitPath), "bin/bash.exe")].find(existsSync);
		if (bashPath) environment.PATH = `${dirname(bashPath)}${delimiter}${environment.PATH}`;
	}
}
if (bashPath) environment.ATLAS_P0_BASH = bashPath;
environment.ATLAS_P0_EVIDENCE = join(runRoot, "probe-events.jsonl");

const groups = {
	coding: ["packages/coding-agent", [
		"test/suite/regressions/2835-tools-allowlist-filters-extension-tools.test.ts",
		"test/suite/regressions/5109-exclude-tools.test.ts",
		"test/suite/regressions/5998-blocked-tool-terminate.test.ts",
		"test/suite/regressions/8935-parallel-preflight-abort.test.ts",
		"test/suite/regressions/1717-2113-agent-session-event-settlement.test.ts",
		"test/rpc-jsonl.test.ts", "test/rpc-client-process-exit.test.ts", "test/rpc-prompt-response-semantics.test.ts",
	]],
	durable: ["packages/durable", [
		"test/harness-tools-recovery.test.ts", "test/harness-submissions.test.ts",
		"test/harness-cancellation-barrier.test.ts", "test/harness-generation-recovery.test.ts",
		"test/harness-storage-failure.test.ts", "test/harness-read-only.test.ts",
	]],
	"coding-p0": ["packages/coding-agent", ["test/suite/atlas-p0-boundaries.test.ts", "test/atlas-p0-sdk-process.test.ts"]],
	"durable-p0": ["packages/durable", ["test/atlas-p0-process-recovery.test.ts"]],
};
const summary = { started: new Date().toISOString(), repository, node: process.version, credentialEnvironment: "allowlist-only; no provider secrets inherited", bashPath, runRoot, results: [] };
const selected = process.argv.slice(2);
if (selected.length === 0) throw new Error("Usage: node scripts/atlas-p0-run.mjs check|coding|durable|coding-p0|durable-p0 [...]");
for (const group of selected) {
	let args;
	let cwd;
	if (group === "check") {
		args = [resolve(process.execPath, "..", "node_modules/npm/bin/npm-cli.js"), "run", "check"];
		cwd = repository;
	} else {
		const config = groups[group];
		if (!config) throw new Error(`Unknown group ${group}`);
		cwd = join(repository, config[0]);
		args = [join(repository, "node_modules/vitest/dist/cli.js"), "--run", ...config[1], "--maxWorkers=2", "--reporter=default", "--reporter=json", `--outputFile.json=${join(runRoot, `${group}.json`)}`];
		if (group.startsWith("durable")) args.push("--config", join(repository, "scripts/atlas-p0-durable.config.ts"));
	}
	const started = Date.now();
	const log = [];
	console.log(`Running ${group}; evidence ${runRoot}`);
	const child = spawn(process.execPath, args, { cwd, env: environment, stdio: ["ignore", "pipe", "pipe"], windowsHide: true });
	for (const [stream, output] of [[child.stdout, process.stdout], [child.stderr, process.stderr]]) {
		stream.on("data", (chunk) => { log.push(chunk); output.write(chunk); });
	}
	const timer = setTimeout(() => { child.kill("SIGKILL"); }, 300000);
	const exitCode = await new Promise((done, fail) => { child.once("error", fail); child.once("close", done); });
	clearTimeout(timer);
	writeFileSync(join(runRoot, `${group}.log`), Buffer.concat(log));
	summary.results.push({ group, exitCode, elapsedMs: Date.now() - started });
	writeFileSync(join(runRoot, "summary.json"), JSON.stringify(summary, null, 2));
}
summary.finished = new Date().toISOString();
writeFileSync(join(runRoot, "summary.json"), JSON.stringify(summary, null, 2));
console.log(`P0 evidence: ${join(runRoot, "summary.json")}`);
if (summary.results.some((result) => result.exitCode !== 0)) process.exitCode = 1;
