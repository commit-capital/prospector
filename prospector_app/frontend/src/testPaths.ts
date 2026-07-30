// Test-file classification and diff size splitting. The rules come from the
// active repository profile, served in RepoMeta.test_paths (/api/meta). Used to
// show the non-test surface area of a change and flag test removals (#17, #22).

export interface TestPathRules { dir_pattern: string; file_pattern: string }

/** Compile the served rules into a matcher, or null if a pattern is not a
 *  valid JS RegExp (the profile's patterns are authored for Python's engine). */
function makeIsTestPath(rules: TestPathRules): ((path: string) => boolean) | null {
  try {
    const dir = new RegExp(rules.dir_pattern, "i");
    const file = new RegExp(rules.file_pattern, "i");
    return (path: string) => {
      if (!path) return false;
      const p = path.trim();
      return dir.test(p) || file.test(p);
    };
  } catch {
    return null;
  }
}

interface SizeBucket { additions: number; deletions: number; files: number }
export interface SizeSplit { test: SizeBucket; non_test: SizeBucket; removes_tests: boolean }

/** Split a unified diff's added/removed line counts into test vs non-test.
 *  Null when the rules don't compile as JS RegExp. */
export function splitDiffSizes(text: string, rules: TestPathRules): SizeSplit | null {
  const isTestPath = makeIsTestPath(rules);
  if (!isTestPath) return null;
  let cur: string | null = null;
  const test = { additions: 0, deletions: 0, files: new Set<string>() };
  const non = { additions: 0, deletions: 0, files: new Set<string>() };
  const bucket = (path: string) => (isTestPath(path) ? test : non);

  for (const line of (text || "").split("\n")) {
    if (line.startsWith("diff --git ")) {
      const tok = line.split(" ").pop() ?? "";
      cur = tok.startsWith("b/") ? tok.slice(2) : tok;
      continue;
    }
    if (line.startsWith("+++ ")) {
      const p = line.slice(4).trim();
      if (p && p !== "/dev/null") cur = p.startsWith("b/") ? p.slice(2) : p;
      continue;
    }
    if (line.startsWith("--- ") || line.startsWith("@@")) continue;
    if (cur == null) continue;
    if (line.startsWith("+")) { const b = bucket(cur); b.additions++; b.files.add(cur); }
    else if (line.startsWith("-")) { const b = bucket(cur); b.deletions++; b.files.add(cur); }
  }

  const shape = (d: typeof test): SizeBucket => ({ additions: d.additions, deletions: d.deletions, files: d.files.size });
  return { test: shape(test), non_test: shape(non), removes_tests: test.deletions > test.additions };
}
