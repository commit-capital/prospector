import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

// Layering invariants for the feedback modal (#95): the overlay must render
// above every other fixed layer — in particular the PR flyout — which takes
// both a top z-index and an escape from the topbar's stacking context.

const stylesCss: string = readFileSync(new URL("../styles.css", import.meta.url), "utf8");
const feedbackButtonSrc: string = readFileSync(
  new URL("./FeedbackButton.tsx", import.meta.url),
  "utf8",
);

/** Every `selector → z-index` pair declared in styles.css. */
function zIndexBySelector(css: string): Map<string, number> {
  const withoutComments = css.replace(/\/\*[\s\S]*?\*\//g, "");
  const out = new Map<string, number>();
  for (const chunk of withoutComments.split("}")) {
    const brace = chunk.indexOf("{");
    if (brace === -1) continue;
    const selector = chunk.slice(0, brace).trim();
    const match = chunk.slice(brace + 1).match(/z-index:\s*(\d+)/);
    if (match) out.set(selector, Number(match[1]));
  }
  return out;
}

test("feedback overlay z-index tops every other layer in styles.css", () => {
  const layers = zIndexBySelector(stylesCss);
  const overlay = layers.get(".feedback-overlay");
  assert.ok(overlay !== undefined, ".feedback-overlay declares a z-index");
  for (const [selector, z] of layers) {
    if (selector === ".feedback-overlay") continue;
    assert.ok(
      overlay >= z,
      `.feedback-overlay (z-index ${overlay}) sits below "${selector}" (z-index ${z})`,
    );
  }
  const flyout = layers.get(".flyout");
  assert.ok(flyout !== undefined, ".flyout declares a z-index");
  assert.ok(overlay > flyout, "the overlay must strictly outrank the PR flyout");
});

test("feedback modal portals to document.body, escaping the topbar stacking context", () => {
  // The feedback button lives in the sticky topbar, whose z-index creates a
  // stacking context that caps every descendant beneath the PR flyout. The
  // overlay's z-index only applies because the modal mounts under document.body.
  assert.match(
    feedbackButtonSrc,
    /createPortal\(\s*<FeedbackModal[\s\S]*?document\.body\s*,?\s*\)/,
    "FeedbackModal must render through createPortal(..., document.body)",
  );
});
