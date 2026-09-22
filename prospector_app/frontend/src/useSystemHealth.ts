import { useEffect, useState } from "react";
import { api, type SystemHealth } from "./api";
import { useRepoMeta } from "./RepoMetaContext";

// How often every page re-asks for deployment health — the strip's one poll.
export const HEALTH_STRIP_POLL_MS = 15_000;

/** Polls /api/status/health on an interval; null until the first answer
 *  lands or while the checkout is unconfigured. Feeds the health strip on
 *  every page and Home's "Stalled" column state. */
export function useSystemHealth(): SystemHealth | null {
  const { meta } = useRepoMeta();
  const configured = meta?.configured ?? false;
  const [health, setHealth] = useState<SystemHealth | null>(null);
  useEffect(() => {
    if (!configured) return;
    const load = () => api.systemHealth().then(setHealth).catch(() => {});
    load();
    const t = setInterval(load, HEALTH_STRIP_POLL_MS);
    return () => clearInterval(t);
  }, [configured]);
  return configured ? health : null;
}
