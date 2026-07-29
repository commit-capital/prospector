// Tracks whether the backend API is reachable. Every request through api.ts
// updates this (see get() / req()), and a background poll of /api/health flips
// it back once the server returns. The app subscribes via useBackendHealth and
// shows a loud banner while it's down — see App.tsx / BackendBanner.

type Listener = (reachable: boolean) => void;

let reachable = true;
const listeners = new Set<Listener>();

// The Vite dev proxy returns these when it can't reach uvicorn upstream; treat
// them as "backend down" rather than an app-level error.
const PROXY_DOWN = new Set([502, 503, 504]);

export function markReachable(ok: boolean): void {
  if (reachable === ok) return;
  reachable = ok;
  for (const l of listeners) l(reachable);
}

export function isReachable(): boolean {
  return reachable;
}

export function subscribeHealth(l: Listener): () => void {
  listeners.add(l);
  return () => { listeners.delete(l); };
}

// Did this Response come back from a dead upstream (proxy 502/503/504)?
export function isProxyDown(status: number): boolean {
  return PROXY_DOWN.has(status);
}

// Poll the health endpoint. Any real HTTP answer (even an error status) means the
// server is up; only a thrown fetch (no connection) or a proxy-down status counts
// as unreachable.
export async function pingHealth(): Promise<void> {
  try {
    const r = await fetch("/api/health", { cache: "no-store" });
    markReachable(!isProxyDown(r.status));
  } catch {
    markReachable(false);
  }
}
