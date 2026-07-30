import type { FilterSpec, PresetName } from "../../api";
import { InfoTip, Term } from "../InfoTip";
import { term } from "../../glossary";

const PRESETS: { v: PresetName; label: string }[] = [
  { v: "easy", label: "⚡ Easy Lane" },
  { v: "stale", label: "🗑️ Stale" },
  { v: "merge-ready", label: "✅ Merge-ready" },
  { v: "needs-human", label: "👤 Needs human" },
];

export function PresetChips({ spec, onChange }:
  { spec: FilterSpec; onChange: (next: FilterSpec) => void }) {
  const active = spec.preset;
  const set = (p: PresetName | undefined) =>
    onChange({ ...spec, preset: p });
  return (
    <div className="chips">
      <span className="label"><Term k="ui.lanes">Lanes</Term></span>
      {PRESETS.map((p) => (
        <InfoTip key={p.v} entry={term(`lane.${p.v}`)} cue={false} focusable={false}>
          <button className={`chip preset ${active === p.v ? "on" : ""}`}
            onClick={() => set(active === p.v ? undefined : p.v)}>{p.label}</button>
        </InfoTip>
      ))}
      <button className={`chip preset ${!active ? "on" : ""}`} onClick={() => set(undefined)}>All open</button>
      {active === "easy" && (
        <span className="preset-knobs">
          ≤ <input type="number" step={25} value={spec.max_effective_loc ?? 150}
            onChange={(e) => onChange({ ...spec, max_effective_loc: Number(e.target.value) })} /> effective lines
        </span>
      )}
      {active === "stale" && (
        <span className="preset-knobs">
          ≥ <input type="number" step={30} value={spec.age_days?.value ?? 60}
            onChange={(e) => onChange({ ...spec, age_days: { op: ">=", value: Number(e.target.value) } })} /> days
        </span>
      )}
    </div>
  );
}
