import { useState } from "react";
import { api, type FilterSpec } from "../../api";
import { useExec } from "../../ExecContext";

// A bare PR number ("620") or "#620" — the query engine already has a purpose-built
// `numbers` filter for this (filters.py), so it never needs the NL→spec translation.
const PR_NUMBER_RE = /^#?(\d+)$/;

export function ExplorerSearchBar({ value, onTextChange, onSpec, onDeepSearch, deepBusy, deepProgress }: {
  value: string;
  onTextChange: (text: string) => void;
  onSpec: (spec: FilterSpec) => void;
  onDeepSearch: (query: string) => void;
  deepBusy?: boolean;
  deepProgress?: { done: number; total: number } | null;
}) {
  const { review } = useExec();
  const example = review.provider !== "none" ? "greptile < 3, " : "";
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState("");
  const run = async () => {
    const q = value.trim();
    if (!q) return;
    const prNumber = q.match(PR_NUMBER_RE);
    if (prNumber) {
      setNote(`interpreted as PR #${prNumber[1]}`);
      onSpec({ numbers: [Number(prNumber[1])] });
      return; // skip the NL-search round trip — a bare number is already the filter
    }
    setBusy(true);
    try {
      const { spec, note } = await api.searchPrs(value);
      setNote(note);
      onSpec(spec);
    } finally { setBusy(false); }
  };
  const deepLabel = deepBusy
    ? `Judging ${deepProgress?.done ?? 0}/${deepProgress?.total ?? "…"}…`
    : "🪄 Deep search";
  return (
    <div className="explorer-search">
      <div className="searchbar">
        <span className="mag">🔍</span>
        <input value={value} placeholder={`e.g. ${example}touches src/auth, non-test lines > 500, or describe what to look for`}
          onChange={(e) => onTextChange(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter") run(); }} />
        <button className="btn-primary sm" onClick={run} disabled={busy}
          title="Map your text to exact filters — instant and deterministic.">{busy ? "…" : "Ask ↵"}</button>
        <button className="btn-secondary sm" onClick={() => onDeepSearch(value)}
          disabled={deepBusy || busy || !value.trim()}
          title="Have the agent read every PR in the current result set and judge it against your text. Slower, costs tokens, and the matches are agent judgments — not deterministic.">
          {deepLabel}
        </button>
      </div>
      {note && <div className="xlate">🪄 {note}</div>}
    </div>
  );
}
