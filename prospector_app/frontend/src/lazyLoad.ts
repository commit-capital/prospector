const RELOAD_MARKER_PREFIX = "prospector:lazy-load-reload:";

function markerKey(id: string): string {
  return `${RELOAD_MARKER_PREFIX}${id}`;
}

function clearMarker(id: string): void {
  try {
    window.sessionStorage.removeItem(markerKey(id));
  } catch {
    // Recovery still has the app-owned error boundary when storage is unavailable.
  }
}

function claimReload(id: string): boolean {
  try {
    const key = markerKey(id);
    if (window.sessionStorage.getItem(key)) return false;
    window.sessionStorage.setItem(key, "1");
    return true;
  } catch {
    return false;
  }
}

/** Load a lazy module, refreshing the page once when its request is rejected. */
export async function loadWithRecovery<T>(id: string, load: () => Promise<T>): Promise<T> {
  try {
    const loaded = await load();
    clearMarker(id);
    return loaded;
  } catch (error: unknown) {
    if (claimReload(id)) {
      window.location.reload();
      return new Promise<T>(() => undefined);
    }
    clearMarker(id);
    throw error;
  }
}
