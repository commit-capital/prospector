import assert from "node:assert/strict";
import { test } from "node:test";
import { fixture } from "./fixture.ts";

test("find a PR, preview a close, and read its durable Activity receipt", { timeout: 90_000 }, async t => {
  const { page, url, requests, api } = await fixture(t);
  await page.go(`${url}/explore`);
  await page.find({ text: "Fix retry counter" }).waitUntil("visible");
  await page.find({ text: "Blocked security fixture" }).waitUntil("visible");
  const search = await page.find('.explorer-search input');
  await search.fill("101");
  await search.press("Enter");
  await page.waitUntil("!document.querySelector('tbody')?.innerText.includes('#102')");
  await page.find({ text: "Fix retry counter" }).click();
  await page.waitUntil("document.body.innerText.includes('Evidence for PR 101')");
  assert.match(await page.find(".flyout").text(), /Fix retry counter/);
  assert.equal(await page.find({ role: "button", text: "DRY RUN" }).isEnabled(), false);

  await page.find('.pr-actions select').selectOption("CLOSE");
  await page.find('.sug-comment-toggle').click();
  const comment = "Closing preview: retry counter is already covered.";
  await page.find({ label: "Comment e2e-bot will post" }).fill(comment);
  await page.find('.pr-tags textarea').fill("Private review note for the fixture");
  await page.find({ role: "button", text: "Close (dry)" }).click();
  await page.waitUntil("document.querySelector('.pr-actions')?.innerText.includes('dry-run')");

  const actions = (await requests()).filter(request => request.path === "/api/execute/pr/101");
  assert.equal(actions.length, 1, "one click sends exactly one action");
  assert.equal(actions[0].query, "dry_run=true");
  assert.deepEqual(actions[0].body, {
    pr: 101, action: "CLOSE", tags: [], reason: "Private review note for the fixture",
    comment, override_stale: false,
  });
  const { items } = await api("/api/activity");
  assert.equal(items.length, 1);
  assert.equal(items[0].pr, 101);
  assert.equal(items[0].action, "CLOSE");
  assert.equal(items[0].status, "dry-run");
  assert.equal(items[0].dry_run, true);
  assert.equal(items[0].identity, "e2e-bot");
  assert.equal(items[0].operator, "E2E Operator");
  assert.match(items[0].detail, /Closing preview: retry counter/);
  assert.equal((await api("/api/prs/101")).github_state, "open", "preview must preserve PR state");

  await page.find({ title: "Close all (Esc)" }).click();
  await page.find('nav a[href="/activity"]').click();
  await page.waitUntil("document.querySelector('.activity-count')?.innerText.includes('1 dry')");
  assert.match(await page.find('.activity-count').text(), /0 succeeded/);
  assert.match(await page.find('tbody').text(), /#101/);
  await page.reload();
  await page.waitUntil("document.querySelector('.activity-count')?.innerText.includes('1 dry')");
  assert.match(await page.find('tbody').text(), /Closing preview: retry counter/);
});

test("a malicious PR shows its merge block and the API enforces it", { timeout: 90_000 }, async t => {
  const { page, url, requests, api } = await fixture(t);
  await page.go(`${url}/explore`);
  await page.find({ text: "Blocked security fixture" }).click();
  await page.find('.pr-actions select').selectOption("MERGE");
  const blocked = await page.find({ role: "button", text: "Merge blocked" });
  assert.equal(await blocked.isEnabled(), false);
  assert.match(await page.find('.pr-actions-wrap').text(), /malicious/i);
  assert.equal((await requests()).filter(request => String(request.path).startsWith("/api/merge/")).length, 0);

  // The disabled control is only the UI guard. Submit the same request over
  // real HTTP to prove the production gate still refuses it independently.
  const response = await fetch(`${url}/api/merge/pr/102?dry_run=true`, { method: "POST" });
  assert.equal(response.status, 200);
  const result = await response.json();
  assert.equal(result.status, "blocked");
  assert.match(result.detail, /malicious/i);
  assert.equal((await api("/api/prs/102")).github_state, "open");
  assert.deepEqual((await api("/api/activity")).items, [], "no successful action receipt");
});
