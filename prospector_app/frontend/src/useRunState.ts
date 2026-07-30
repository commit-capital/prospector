import { useCallback, useEffect, useState } from "react";
import { api, type RunState } from "./api";
import { useExec } from "./ExecContext";

export interface RunStateMap {
  byPr: Record<number, RunState>;
  refresh: () => void;
}

/** Background lookup of which of these PRs already have a landed live action
 *  (#10), so the UI can mark them done and guard a re-fire. Log-backed, so it
 *  survives a refresh. Refetches automatically whenever a global action lands
 *  or the dry-run mode flips (actionTick, #67/#69). Returns a `refresh()` too. */
export function useRunState(prs: number[]): RunStateMap {
  const { actionTick } = useExec();
  const [byPr, setByPr] = useState<Record<number, RunState>>({});
  const key = prs.slice().sort((a, b) => a - b).join(",");

  const load = useCallback(() => {
    if (!prs.length) { setByPr({}); return; }
    api.runState(prs).then((r) => setByPr(r.states ?? {})).catch(() => {});
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);

  useEffect(() => { load(); }, [load, actionTick]); // eslint-disable-line react-hooks/set-state-in-effect -- load() fetches run-state on mount/refresh
  return { byPr, refresh: load };
}
