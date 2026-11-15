import { readFileSync } from "node:fs";
import { expect, test } from "vitest";

test("canonical revision strings survive the web JSON boundary", () => {
  const examples: { id: string; value: unknown }[] = JSON.parse(
    readFileSync("packages/contracts/examples/common-examples.json", "utf8"),
  );
  const revision = examples.find((example) => example.id === "resource-revision-maximum")?.value;
  expect(revision).toBe("9223372036854775807");
  const wire = JSON.stringify({ expected_revision: revision });
  const received: { expected_revision: unknown } = JSON.parse(wire);
  expect(received.expected_revision).toBe(revision);
  expect(typeof received.expected_revision).toBe("string");
});
