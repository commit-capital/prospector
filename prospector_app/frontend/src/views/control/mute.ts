/** Needs-you items an operator muted, per browser, each for MUTE_MS. Storage
 *  that is blocked or throws mutes nothing. */

const KEY = "control.mutes";
export const MUTE_MS = 24 * 3_600_000;

export function readMutes(storage: () => Pick<Storage, "getItem"> = () => localStorage): Record<string, number> {
  try {
    const v = JSON.parse(storage().getItem(KEY) ?? "{}") as unknown;
    return v && typeof v === "object" ? (v as Record<string, number>) : {};
  } catch {
    return {};
  }
}

export function mute(key: string, now: number,
  storage: () => Pick<Storage, "getItem" | "setItem"> = () => localStorage): void {
  try {
    const live = Object.fromEntries(Object.entries(readMutes(storage)).filter(([, at]) => now - at < MUTE_MS));
    storage().setItem(KEY, JSON.stringify({ ...live, [key]: now }));
  } catch {
    // Unsaved: the item shows again.
  }
}

export function isMuted(key: string, mutes: Record<string, number>, now: number): boolean {
  const at = mutes[key];
  return at != null && now - at < MUTE_MS;
}
