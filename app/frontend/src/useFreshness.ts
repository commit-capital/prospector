import { useCallback, useEffect, useRef, useState } from "react";
import { api, type Divergence } from "./api";

export interface FreshnessState {
  status: "idle" | "loading" | "done" | "error";
  at?: string;
  byPr: Record<number, Divergence[]>;
  changed: number;
  refresh: () => void;
}

/** On mount (and whenever the PR set changes) kick off a background live-state
 *  refresh for the given PRs and report which ones have diverged from the
 *  app's snapshot (#25). Read-only; one batched request. Exposes `refresh()`
 *  so a caller can force a re-check after landing a fix (e.g. a re-analysis run
 *  that catches the stored head up to what diverged). */
export function useFreshness(prs: number[]): FreshnessState {
  const [state, setState] = useState<Omit<FreshnessState, "refresh">>({ status: "idle", byPr: {}, changed: 0 });
  const key = prs.slice().sort((a, b) => a - b).join(",");
  // guards against an older in-flight request clobbering a newer one's result.
  const seq = useRef(0);

  const load = useCallback(() => {
    if (!prs.length) { setState({ status: "idle", byPr: {}, changed: 0 }); return; }
    const id = ++seq.current;
    setState((s) => ({ ...s, status: "loading" }));
    api.freshness(prs).then((r) => {
      if (id !== seq.current) return;
      const byPr: Record<number, Divergence[]> = {};
      for (const it of r.items) if (it.diverged?.length) byPr[it.number] = it.diverged;
      setState({ status: "done", at: new Date().toLocaleTimeString([], { hour: "numeric", minute: "2-digit" }),
        byPr, changed: Object.keys(byPr).length });
    }).catch(() => { if (id === seq.current) setState((s) => ({ ...s, status: "error" })); });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);

  useEffect(() => { load(); }, [load]); // eslint-disable-line react-hooks/set-state-in-effect -- initial load + reload on PR-set change

  return { ...state, refresh: load };
}
