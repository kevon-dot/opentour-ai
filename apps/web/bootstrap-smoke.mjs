// Dependency/package smoke only; this is not an application route or UI acceptance test.
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { packageBoundary } from "@opentour/client";
const require = createRequire(import.meta.url);
assert.equal(process.versions.node, "24.22.3");
assert.equal(require("next/package.json").version, "16.4.6");
assert.equal(require("react/package.json").version, "19.3.1");
assert.equal(packageBoundary, "generated-client-unimplemented");
console.log(JSON.stringify({ kind: "local_integration", check: "web-package-import", node: process.versions.node, status: "PASS", product_acceptance: "NOT_RUN" }));
