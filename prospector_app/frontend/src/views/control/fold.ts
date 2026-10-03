/** Whether a Control-tab panel is open, remembered per browser. Storage that
 *  is blocked or throws leaves every panel at its default. */

const KEY = (id: string): string => `control.panel.${id}`;

export function readFold(id: string, fallback: boolean,
  storage: () => Pick<Storage, "getItem"> = () => localStorage): boolean {
  try {
    const v = storage().getItem(KEY(id));
    return v === "1" ? true : v === "0" ? false : fallback;
  } catch {
    return fallback;
  }
}

export function writeFold(id: string, open: boolean,
  storage: () => Pick<Storage, "setItem"> = () => localStorage): void {
  try {
    storage().setItem(KEY(id), open ? "1" : "0");
  } catch {
    // Unsaved: the panel opens at its default next time.
  }
}
