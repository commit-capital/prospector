import { createContext, useContext, useEffect, useState, type ReactNode } from "react";
import { api, type WorkStatus } from "./api";

/** One /api/status/now poll shared by the header badge, the global health
 *  strip, and the Home cards' stalled state. `null` until the first poll lands
 *  (or while the backend refuses the route on an unconfigured checkout). */
const Ctx = createContext<WorkStatus | null>(null);

const POLL_MS = 15_000;

// eslint-disable-next-line react-refresh/only-export-components -- context hook co-located with its provider
export const useWorkStatus = () => useContext(Ctx);

export function WorkStatusProvider({ children }: { children: ReactNode }) {
  const [status, setStatus] = useState<WorkStatus | null>(null);
  useEffect(() => {
    let live = true;
    const load = () => api.workStatus().then((s) => { if (live) setStatus(s); }).catch(() => {});
    load();
    const t = setInterval(load, POLL_MS);
    return () => { live = false; clearInterval(t); };
  }, []);
  return <Ctx.Provider value={status}>{children}</Ctx.Provider>;
}
