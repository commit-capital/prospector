import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

const appSource = readFileSync(new URL("./App.tsx", import.meta.url), "utf8");
const homeSource = readFileSync(new URL("./views/Home.tsx", import.meta.url), "utf8");

test("the landing tab is named Home in the nav, the view title, and its heading", () => {
  assert.match(appSource, /<NavLink to="\/" end>Home<\/NavLink>/);
  assert.match(appSource, /\["\/", "Home"\]/);
  assert.match(homeSource, /<h2>Home<\/h2>/);
});

test("no surface calls the landing tab an Inbox", () => {
  assert.doesNotMatch(appSource, /\bInbox\b/);
  assert.doesNotMatch(homeSource, /\bInbox\b/);
});
