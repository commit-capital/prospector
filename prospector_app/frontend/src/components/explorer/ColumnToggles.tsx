import { COLUMNS } from "./columns";
import { Term } from "../InfoTip";
import { useExec } from "../../ExecContext";

// A chip row (matching Lanes/Filters) toggling which non-anchor columns show.
export function ColumnToggles({ isOn, toggle, reset }: {
  isOn: (k: string) => boolean;
  toggle: (k: string) => void;
  reset: () => void;
}) {
  const { activeReviewers } = useExec();
  return (
    <div className="chips">
      <span className="label"><Term k="ui.columns">Columns</Term></span>
      {COLUMNS.filter((c) => !c.fixed && (!c.capability || activeReviewers(c.capability).length > 0)).map((c) => (
        <button key={c.key} className={`chip toggle ${isOn(c.key) ? "on" : ""}`}
          onClick={() => toggle(c.key)}>{c.label}</button>
      ))}
      <button className="link-btn" onClick={reset}>Reset</button>
    </div>
  );
}
