// Shared structured reason tags — one click, captured as training labels.
const DECISION_TAGS = [
  "duplicate", "superseded", "already-fixed", "out-of-scope", "wrong-approach",
  "incomplete", "untested", "risky", "low-quality", "needs-revision",
  "clean", "well-tested",
];

/** Quick multi-select tag chips for capturing a structured decision reason. */
export function TagPicker({ selected, onToggle }: { selected: string[]; onToggle: (t: string) => void }) {
  return (
    <div className="tagpicker">
      {DECISION_TAGS.map((t) => (
        <button key={t} type="button"
          className={`tag-chip ${selected.includes(t) ? "on" : ""}`}
          onClick={() => onToggle(t)}>
          {t}
        </button>
      ))}
    </div>
  );
}
