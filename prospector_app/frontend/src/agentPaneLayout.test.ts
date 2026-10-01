import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

// Layout invariants for the fixed left agent pane (#400, #403). The pane starts
// just below the sticky app header — the banners and the topbar together — and
// `.app` reserves its width as a left gutter, so nothing else may reach into
// that gutter, and a table wider than the page
// scrolls inside its own box so the page never scrolls sideways under the pane.

const stylesCss: string = readFileSync(new URL("./styles.css", import.meta.url), "utf8");
const appSrc: string = readFileSync(new URL("./App.tsx", import.meta.url), "utf8");
const prExplorerSrc: string = readFileSync(new URL("./views/PRExplorer.tsx", import.meta.url), "utf8");
const issuesSrc: string = readFileSync(new URL("./views/Issues.tsx", import.meta.url), "utf8");

/** Selectors of every rule in `css` whose declarations match `pattern`. */
function selectorsDeclaring(css: string, pattern: RegExp): string[] {
  const withoutComments = css.replace(/\/\*[\s\S]*?\*\//g, "");
  const out: string[] = [];
  for (const chunk of withoutComments.split("}")) {
    const brace = chunk.indexOf("{");
    if (brace === -1) continue;
    if (pattern.test(chunk.slice(brace + 1))) out.push(chunk.slice(0, brace).trim());
  }
  return out;
}

test("only the app header, which sits above the agent pane, spans the pane's gutter", () => {
  const spanning = selectorsDeclaring(stylesCss, /margin-left:\s*calc\(\s*-1\s*\*\s*var\(--ap-w/);
  assert.deepEqual(spanning, [".app-head"]);
});

test("the banners and the topbar share the one sticky header", () => {
  assert.match(appSrc, /<div className="app-head">\s*<BackendBanner \/>\s*<StoreWriteBanner \/>\s*<header className="topbar">/);
  const sticky = selectorsDeclaring(stylesCss, /position:\s*sticky/);
  assert.ok(sticky.includes(".app-head"), ".app-head is sticky");
  for (const part of [".topbar", ".backend-down", ".backend-loading", ".store-write-block"]) {
    assert.ok(!sticky.includes(part), `${part} sticks only as part of .app-head`);
  }
});

test("--topbar-h measures the whole header, banners included", () => {
  assert.match(appSrc, /querySelector<HTMLElement>\("\.app-head"\)[\s\S]{0,200}setProperty\("--topbar-h"/);
});

test("the agent pane starts at the header's measured height", () => {
  const pane = selectorsDeclaring(stylesCss, /position:\s*fixed[\s\S]*top:\s*var\(--topbar-h/);
  assert.ok(pane.includes(".agentpane"), ".agentpane is fixed below --topbar-h");
});

for (const [name, src, table] of [
  ["PR explorer", prExplorerSrc, "pr-table"],
  ["Issues", issuesSrc, "issues-table"],
] as const) {
  test(`the ${name} table scrolls inside a .table-scroll wrapper`, () => {
    assert.match(src, new RegExp(`<div className="table-scroll"[^>]*>\\s*<table className="grid sortable ${table}"`));
  });
}

test(".table-scroll scrolls horizontally", () => {
  assert.ok(selectorsDeclaring(stylesCss, /overflow-x:\s*auto/).includes(".table-scroll"));
});
