#!/usr/bin/env node
// Stub of upstream's run-vitest-stable.mjs, exposing exactly the surface
// verify-suite.mjs consumes: the two --dry-run plan outputs and the
// nonServerProjects array literal its source scan extracts.
const nonServerProjects = ["@fix/ui", "@fix/lib"];
const generalWorkspacesAProjects = ["@fix/ui"];
void generalWorkspacesAProjects;
void nonServerProjects;
const args = process.argv.slice(2);
if (!args.includes("--dry-run")) {
  console.error("stub wrapper supports only --dry-run");
  process.exit(1);
}
if (args.includes("--group")) {
  console.log(JSON.stringify({
    selectedGeneralServerSuites: [
      "server/src/__tests__/gs-one.test.ts",
      "server/src/__tests__/gs-two.test.ts",
    ],
  }));
} else {
  console.log(JSON.stringify({
    selectedSerializedSuites: [
      "server/src/__tests__/route-a.test.ts",
      "server/src/__tests__/authz-b.test.ts",
    ],
  }));
}
