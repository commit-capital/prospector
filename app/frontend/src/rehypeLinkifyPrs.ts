// rehype plugin: turn PR references in rendered rationale text into
// <a class="pr-ref" data-pr="N"> nodes the ClusterDetail `a` override wires to the
// PR flyout. Two cases:
//   - "#1234"  — an explicit ref, always linked (covers upstream PRs that aren't
//                cluster members, e.g. a superseding PR).
//   - "1234"   — a BARE number, linked only when it's one of `known` (the cluster's
//                member PRs). Rationales often write members without the '#', and the
//                reformat pass may not add one (it can't change the words), so without
//                this most refs would never link. Membership-gating avoids linking
//                dates/ratios/counts; the boundary guards exclude `2026-04-16`, SHAs,
//                versions, etc.
// Render-time only — the stored rationale stays plain text, so the faithfulness gate
// that proves it's verbatim never reasons about link syntax. Skips code/pre/links.
// Dependency-free hast walk (pnpm's strict layout won't resolve a bare
// unist-util-visit import).

interface HastNode {
  type: string;
  tagName?: string;
  value?: string;
  children?: HastNode[];
  properties?: Record<string, unknown>;
}

const text = (value: string): HastNode => ({ type: "text", value });
const anchor = (pr: string, label: string): HastNode => ({
  type: "element",
  tagName: "a",
  properties: { className: ["pr-ref"], dataPr: pr },
  children: [text(label)],
});

// #1234 (explicit) OR a standalone 2-6 digit run not glued to a word/./:/-/ char
// (so it never fires inside a date, SHA, version, or ratio).
const REF_RE = /#(\d+)|(?<![\w./:-])(\d{2,6})(?![\w./:-])/g;

function splitText(value: string, known: Set<number>): HastNode[] {
  REF_RE.lastIndex = 0;
  const parts: HastNode[] = [];
  let last = 0;
  let m: RegExpExecArray | null;
  while ((m = REF_RE.exec(value)) !== null) {
    const explicit = m[1];
    const bare = m[2];
    const linkable = explicit != null || (bare != null && known.has(Number(bare)));
    if (!linkable) continue;
    if (m.index > last) parts.push(text(value.slice(last, m.index)));
    parts.push(anchor(explicit ?? bare, m[0]));
    last = m.index + m[0].length;
  }
  if (last < value.length) parts.push(text(value.slice(last)));
  return parts.length ? parts : [text(value)];
}

function walk(node: HastNode, parentTag: string | null, known: Set<number>): void {
  if (!node.children) return;
  const skip = parentTag === "code" || parentTag === "pre" || parentTag === "a";
  const out: HastNode[] = [];
  for (const child of node.children) {
    if (child.type === "element") walk(child, child.tagName ?? null, known);
    if (child.type === "text" && !skip && /\d/.test(child.value ?? "")) {
      out.push(...splitText(child.value ?? "", known));
    } else {
      out.push(child);
    }
  }
  node.children = out;
}

/** Build a rehype plugin that linkifies PR refs, gating bare numbers on `known`. */
export function rehypeLinkifyPrs(known: Set<number>) {
  return () => (tree: HastNode): void => walk(tree, null, known);
}
