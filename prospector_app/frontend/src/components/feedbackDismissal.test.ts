import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

// Dismissal and draft invariants for the feedback modal (#120): a drag that
// begins inside the modal (text selection, textarea resize) must not dismiss
// it when the mouse releases over the backdrop, and the typed description
// must survive every dismissal path.

const feedbackButtonSrc: string = readFileSync(
  new URL("./FeedbackButton.tsx", import.meta.url),
  "utf8",
);

test("overlay dismissal requires the press to start on the backdrop", () => {
  // A click event fires on the overlay whenever a drag releases over it, even
  // when the drag began inside the modal. The mousedown handler records where
  // the press started, and the click handler closes only when both the press
  // and the release landed directly on the backdrop.
  assert.match(
    feedbackButtonSrc,
    /handleOverlayMouseDown[\s\S]*?pressStartedOnOverlay\.current\s*=\s*e\.target\s*===\s*e\.currentTarget/,
    "the overlay mousedown handler must record whether the press began on the backdrop",
  );
  assert.match(
    feedbackButtonSrc,
    /handleOverlayClick[\s\S]*?e\.target\s*===\s*e\.currentTarget\s*&&\s*pressStartedOnOverlay\.current/,
    "the overlay click handler must require the press to have started on the backdrop",
  );
  assert.match(
    feedbackButtonSrc,
    /className="feedback-overlay"[\s\S]*?onMouseDown=\{handleOverlayMouseDown\}[\s\S]*?onClick=\{handleOverlayClick\}/,
    "the overlay element must wire both the mousedown and click handlers",
  );
});

test("the typed description is drafted to localStorage and restored on reopen", () => {
  assert.match(
    feedbackButtonSrc,
    /useState\(\(\)\s*=>\s*localStorage\.getItem\(DRAFT_KEY\)\s*\?\?\s*""\)/,
    "the description state must initialize from the saved draft",
  );
  assert.match(
    feedbackButtonSrc,
    /localStorage\.setItem\(DRAFT_KEY,\s*description\)/,
    "the description must be persisted as the user types",
  );
});

test("the saved draft is cleared once the issue opens", () => {
  assert.match(
    feedbackButtonSrc,
    /if\s*\(url\)\s*localStorage\.removeItem\(DRAFT_KEY\)/,
    "a successful Open Issue must drop the saved draft",
  );
});
