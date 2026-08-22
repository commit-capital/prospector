import assert from "node:assert/strict";
import { readdirSync, readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { join } from "node:path";
import { test } from "node:test";

// JSX text renders a \uXXXX escape verbatim ("run history ·"), so visible
// characters go into .tsx sources as literal glyphs. An escape is allowed only
// for a character with no visible glyph (whitespace such as the no-break space).
const srcDir: string = fileURLToPath(new URL(".", import.meta.url));

function tsxFiles(dir: string): string[] {
  return readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    const full = join(dir, entry.name);
    if (entry.isDirectory()) return tsxFiles(full);
    return entry.isFile() && entry.name.endsWith(".tsx") ? [full] : [];
  });
}

test("visible characters appear literally in tsx sources, never as \\u escapes", () => {
  const violations: string[] = [];
  for (const file of tsxFiles(srcDir)) {
    const src = readFileSync(file, "utf8");
    for (const m of src.matchAll(/\\u(?:([0-9a-fA-F]{4})|\{([0-9a-fA-F]+)\})/g)) {
      const ch = String.fromCodePoint(parseInt(m[1] ?? m[2], 16));
      if (/\s/u.test(ch)) continue;
      const line = src.slice(0, m.index).split("\n").length;
      violations.push(`${file.slice(srcDir.length)}:${line} ${m[0]} — write the character itself (${ch})`);
    }
  }
  assert.deepEqual(violations, []);
});
