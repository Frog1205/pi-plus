import { mergeConfig } from "vitest/config";
import durable from "../packages/durable/vitest.config.ts";
import sourceAliases from "../vitest.base.ts";

// The durable package normally consumes built workspace packages. P0 instead
// resolves the exact checkout sources, just as the coding-agent test config does.
export default mergeConfig(sourceAliases, durable);
