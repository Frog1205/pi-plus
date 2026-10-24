import { writeFileSync } from "node:fs";
import { join } from "node:path";

const rootPath = process.argv[2];
if (!rootPath) throw new Error("Missing owned fixture directory");
// This deliberately lacks a parent-death mechanism. The P0 test proves that
// killing a Worker is not equivalent to containing all of its child processes.
writeFileSync(join(rootPath, "child-started.txt"), String(process.pid));
setTimeout(() => {
	writeFileSync(join(rootPath, "late-child-effect.txt"), "FIXTURE_CHILD_EFFECT");
}, 1500);
