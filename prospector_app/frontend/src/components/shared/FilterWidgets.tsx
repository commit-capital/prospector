import type { NumCmp } from "../../api";

// Generic column-filter-popout building blocks shared by the PR Explorer and
// Issues per-column filters (#494) — neither widget knows about PR or Issue
// fields specifically, only the {op,value} / enum shapes their callers pass in.

export function EnumFilter<T extends string>({
  opts, current, onChange,
}: {
  opts: { v: T | ""; label: string }[];
  current: T | T[] | undefined;
  onChange: (v: T | T[] | undefined) => void;
}) {
  const selected: T[] = current === undefined ? []
    : Array.isArray(current) ? current
    : [current];

  const toggle = (v: T | "") => {
    if (!v) {
      onChange(undefined);
      return;
    }
    const next = selected.includes(v as T)
      ? selected.filter((x) => x !== v)
      : [...selected, v as T];
    onChange(next.length === 0 ? undefined : next.length === 1 ? next[0] : next);
  };

  return (
    <div className="cfp-opts">
      {opts.map(({ v, label }) => {
        const isActive = v === "" ? selected.length === 0 : selected.includes(v as T);
        return (
          <button
            key={v || "__any__"}
            className={`cfp-opt${isActive ? " active" : ""}`}
            onClick={() => toggle(v)}
          >{label}</button>
        );
      })}
    </div>
  );
}

export function NumFilter({
  cmp, onCmp,
  label = "", placeholder = "value", min = 0, step = 1,
  convertDisplay, convertStore,
}: {
  cmp: NumCmp | undefined;
  onCmp: (v: NumCmp | undefined) => void;
  label?: string;
  placeholder?: string;
  min?: number;
  step?: number;
  convertDisplay?: (v: number) => number;
  convertStore?: (s: string) => number;
}) {
  const displayVal = cmp?.value != null
    ? (convertDisplay ? convertDisplay(cmp.value) : cmp.value)
    : "";
  return (
    <div className="cfp-row">
      {label && <span className="cfp-row-label">{label}</span>}
      <select
        className="cfp-select"
        value={cmp?.op ?? ">"}
        onChange={(e) => onCmp({ op: e.target.value as NumCmp["op"], value: cmp?.value as number })}
      >
        <option value=">">above</option>
        <option value="<">below</option>
      </select>
      <input
        type="number"
        className="cfp-num"
        min={min}
        step={step}
        placeholder={placeholder}
        value={displayVal}
        onChange={(e) => {
          if (e.target.value === "") {
            onCmp(undefined);
          } else {
            const stored = convertStore ? convertStore(e.target.value) : Number(e.target.value);
            onCmp({ op: cmp?.op ?? ">", value: stored });
          }
        }}
      />
      {cmp !== undefined && (
        <button type="button" className="cfp-clear" title="Clear this filter" onClick={() => onCmp(undefined)}>✕</button>
      )}
    </div>
  );
}
