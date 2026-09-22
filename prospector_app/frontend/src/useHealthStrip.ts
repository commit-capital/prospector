import { useSyncExternalStore } from "react";
import { api, type HealthStripStatus } from "./api";

// One shared poll of /api/health/strip for every subscriber (the global strip
// and Home's worker column), so N consumers cost one request per cycle. A
// failed poll keeps the last answer — the backend banner covers a dead API.
const POLL_MS = 15_000;

let current: HealthStripStatus | null = null;
const listeners = new Set<() => void>();
let timer: number | undefined;

async function poll(): Promise<void> {
  try {
    current = await api.healthStrip();
    for (const l of listeners) l();
  } catch {
    // keep the last answer
  }
}

function subscribe(onChange: () => void): () => void {
  listeners.add(onChange);
  if (timer === undefined) {
    void poll();
    timer = window.setInterval(() => void poll(), POLL_MS);
  }
  return () => {
    listeners.delete(onChange);
    if (listeners.size === 0 && timer !== undefined) {
      window.clearInterval(timer);
      timer = undefined;
    }
  };
}

function subscribeDisabled(): () => void {
  return () => {};
}

function snapshot(): HealthStripStatus | null {
  return current;
}

function snapshotDisabled(): null {
  return null;
}

/** The health strip's latest answer, polled while any subscriber is mounted.
 *  Pass `enabled: false` to sit out (an unconfigured checkout has no strip). */
export function useHealthStrip(enabled = true): HealthStripStatus | null {
  return useSyncExternalStore(
    enabled ? subscribe : subscribeDisabled,
    enabled ? snapshot : snapshotDisabled,
  );
}
