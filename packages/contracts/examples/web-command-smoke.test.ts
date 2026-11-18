import { readFileSync } from "node:fs";
import assert from 'node:assert/strict';
import { test } from 'node:test';

test("canonical revision strings survive the web JSON boundary", () => {
  const examples: { id: string; value: unknown }[] = JSON.parse(
    readFileSync("packages/contracts/examples/common-examples.json", "utf8"),
  );
  const revision = examples.find((example) => example.id === "resource-revision-maximum")?.value;
  assert.equal(revision, "9223372036854775807");
  const wire = JSON.stringify({ expected_revision: revision });
  const received: { expected_revision: unknown } = JSON.parse(wire);
  assert.equal(received.expected_revision, revision);
  assert.equal(typeof received.expected_revision, "string");
});
