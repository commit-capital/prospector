import assert from "node:assert/strict";
import test from "node:test";

import { addSubjectParams, subjectFromLocation } from "./agentSubject.ts";

test("selected advisories become chat subjects", () => {
  assert.deepEqual(
    subjectFromLocation("/alerts", "?advisory=GHSA-2222-3333-4444"),
    {
      kind: "advisory", key: "advisory:GHSA-2222-3333-4444",
      ghsa: "GHSA-2222-3333-4444", label: "advisory GHSA-2222-3333-4444",
    },
  );
});

test("existing numbered subjects retain precise fields", () => {
  assert.deepEqual(subjectFromLocation("/explore", "?pr=42"), {
    kind: "pr", key: "pr:42", number: 42, label: "PR #42",
  });
  assert.deepEqual(subjectFromLocation("/issues", "?issue=9"), {
    kind: "issue", key: "issue:9", number: 9, label: "issue #9",
  });
  assert.deepEqual(subjectFromLocation("/clusters/7", ""), {
    kind: "cluster", key: "cluster:7", number: 7, label: "cluster 7",
  });
});

test("selected alerts become chat subjects", () => {
  const subject = subjectFromLocation("/alerts", "?alert_source=dependabot&alert=17");
  assert.deepEqual(subject, {
    kind: "alert", key: "alert:dependabot:17", source: "dependabot", number: 17,
    label: "dependabot alert #17",
  });
  const params = new URLSearchParams();
  addSubjectParams(params, subject);
  assert.equal(params.toString(), "alert_source=dependabot&alert=17");
});

test("security selection params do not affect other views", () => {
  assert.equal(
    subjectFromLocation("/issues", "?advisory=GHSA-2222-3333-4444").kind,
    "general",
  );
});

test("invalid identifiers fall back to general context", () => {
  assert.equal(subjectFromLocation("/explore", "?pr=not-a-number").kind, "general");
  assert.equal(
    subjectFromLocation("/alerts", "?alert_source=unknown&alert=17").kind,
    "general",
  );
});
