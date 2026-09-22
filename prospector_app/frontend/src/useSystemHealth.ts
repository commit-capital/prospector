import { useEffect, useState } from "react";
import { api, type SystemHealth } from "./api";

// One shared poll of /api/system-health for every subscriber (the strip on
// every page plus Home's stalled state), so opening more views never adds
// requests. Polling runs only while someone is subscribed.

const POLL_MS = 15_000;

let current: SystemHealth | null = null;
const listeners = new Set<(h: SystemHealth | null) => void>();
let timer: number | undefined;

async function poll(): Promise<void> {
  try {
    current = await api.systemHealth();
  } catch {
    current = null; // backend down or unconfigured — nothing to show
  }
  for (const l of listeners) l(current);
}

function sync(): void {
  if (listeners.size > 0 && timer === undefined) {
    void poll();
    timer = window.setInterval(() => { void poll(); }, POLL_MS);
  } else if (listeners.size === 0 && timer !== undefined) {
    window.clearInterval(timer);
    timer = undefined;
  }
}

/** The latest systemwide health answer, re-rendered on every poll; null until
 *  the first poll lands or while the backend is unreachable. */
export function useSystemHealth(): SystemHealth | null {
  const [health, setHealth] = useState<SystemHealth | null>(current);
  useEffect(() => {
    listeners.add(setHealth);
    sync();
    return () => { listeners.delete(setHealth); sync(); };
  }, []);
  return health;
}
