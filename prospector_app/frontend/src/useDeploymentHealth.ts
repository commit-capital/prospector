import { useEffect, useState } from "react";
import { api, type DeploymentHealth } from "./api";

// One shared poll of /api/status/health feeds every subscriber — the strip on
// every page and the Home columns — so N mounted consumers cost one request
// per interval. `null` until the first poll lands or while the endpoint is
// unreachable, which subscribers read as "nothing to report".
const POLL_MS = 15_000;

let current: DeploymentHealth | null = null;
const listeners = new Set<(h: DeploymentHealth | null) => void>();
let timer: number | undefined;

async function poll(): Promise<void> {
  try {
    current = await api.deploymentHealth();
  } catch {
    current = null;
  }
  for (const l of listeners) l(current);
}

export function useDeploymentHealth(): DeploymentHealth | null {
  const [health, setHealth] = useState<DeploymentHealth | null>(current);
  useEffect(() => {
    listeners.add(setHealth);
    if (listeners.size === 1) {
      void poll();
      timer = window.setInterval(() => { void poll(); }, POLL_MS);
    }
    return () => {
      listeners.delete(setHealth);
      if (listeners.size === 0) window.clearInterval(timer);
    };
  }, []);
  return health;
}
