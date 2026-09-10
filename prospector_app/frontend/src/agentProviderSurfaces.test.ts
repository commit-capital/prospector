import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

const setupSource = readFileSync(new URL("./views/Setup.tsx", import.meta.url), "utf8");
const welcomeSource = readFileSync(new URL("./views/Welcome.tsx", import.meta.url), "utf8");
const chooserSource = readFileSync(
  new URL("./components/AgentProviderChooser.tsx", import.meta.url),
  "utf8",
);

test("the agent provider chooser is present in onboarding and permanent setup", () => {
  assert.match(welcomeSource, /<AgentProviderChooser/);
  assert.match(setupSource, /<AgentProviderSettings\s*\/>/);
  assert.match(setupSource, /<AgentProviderChooser/);
  assert.match(setupSource, /step: "agent"/);
});

test("the shared chooser offers every supported setting", () => {
  assert.match(chooserSource, /onPick\("claude"\)/);
  assert.match(chooserSource, /onPick\("codex"\)/);
  assert.match(chooserSource, /onPick\("none"\)/);
});
