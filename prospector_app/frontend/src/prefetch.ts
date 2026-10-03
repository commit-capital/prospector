import { runRead } from "./readCache";
import { TAB_VIEWS } from "./tabViews";
import { ISSUE_TABLE_FIRST_PAGE, issueTableRead } from "./views/issueTable";

// Once the first page has settled, read the other tabs ahead so their first
// visit paints at once: each tab's code, then the Issues table's first page,
// the one first screen slow to read. One read at a time, so the page in front
// keeps the browser's connections it needs.

let started = false;

export async function prefetchTabs(): Promise<void> {
  if (started) return;
  started = true;
  for (const load of TAB_VIEWS) {
    try {
      await load();
    } catch {
      // The tab loads its code again when it is opened.
    }
  }
  try {
    await runRead(issueTableRead(ISSUE_TABLE_FIRST_PAGE));
  } catch {
    // The Issues view reads its own first page.
  }
}
