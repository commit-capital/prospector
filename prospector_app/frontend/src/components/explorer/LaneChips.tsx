import type { FilterSpec } from "../../api";
import { InfoTip, Term } from "../InfoTip";
import { term } from "../../glossary";
import { LANES } from "./lanes";

// Lane chips drop a filter template into the spec (see lanes.ts). A chip
// lights up while the spec still equals its template exactly; editing any
// filter dims it, since the view is then the operator's own query.
export function LaneChips({ spec, onChange }:
  { spec: FilterSpec; onChange: (next: FilterSpec) => void }) {
  const specKey = JSON.stringify(spec);
  return (
    <div className="chips">
      <span className="label"><Term k="ui.lanes">Lanes</Term></span>
      {LANES.map((l) => {
        const on = specKey === JSON.stringify(l.spec);
        return (
          <InfoTip key={l.key} entry={term(`lane.${l.key}`)} cue={false} focusable={false}>
            <button className={`chip lane ${on ? "on" : ""}`}
              onClick={() => onChange(on ? {} : structuredClone(l.spec))}>{l.label}</button>
          </InfoTip>
        );
      })}
      <button className={`chip lane ${specKey === "{}" ? "on" : ""}`}
        onClick={() => onChange({})}>All open</button>
    </div>
  );
}
