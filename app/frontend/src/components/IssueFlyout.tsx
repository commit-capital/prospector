import { useEffect, useState } from "react";
import { useIssueFlyout } from "../useIssueFlyout";
import { useRepoMeta } from "../RepoMetaContext";
import { useResizableWidth } from "../useResizableWidth";
import { IssueDetailContent } from "../views/IssueDetail";

/** Right-side flyout that shows one issue's details over whatever page you're
 *  on. Open via ?issue=N (set by IssueLink); ESC or ✕ closes. Drag the left
 *  edge to resize. The ↗ button opens the issue on GitHub. */
export function IssueFlyout() {
  const { issue, close } = useIssueFlyout();
  const { issueUrl } = useRepoMeta();
  const [maximized, setMaximized] = useState(false);
  const { width, startResize } = useResizableWidth("app-flyout-width", 640);

  useEffect(() => {
    if (issue == null) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") close(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [issue, close]);

  if (issue == null) return null;

  return (
    <>
      <div className="flyout-scrim" onClick={close} />
      <aside className={`flyout ${maximized ? "flyout-max" : ""}`}
        role="dialog" aria-label={`Issue ${issue}`}
        style={maximized ? undefined : { width }}>
        <div className="flyout-resize" onMouseDown={startResize} title="Drag to resize" role="separator" aria-orientation="vertical" />
        <div className="flyout-bar">
          <button className="flyout-btn" title={maximized ? "Restore" : "Maximize"} onClick={() => setMaximized((m) => !m)}>
            {maximized ? "⤡" : "⤢"}
          </button>
          <a className="flyout-btn" title="Open on GitHub"
             href={issueUrl(issue)} target="_blank" rel="noreferrer">↗</a>
          <button className="flyout-btn flyout-close" title="Close (Esc)" onClick={close}>✕</button>
        </div>
        <div className="flyout-panes">
          <section className="flyout-pane">
            <div className="flyout-body">
              <IssueDetailContent key={issue} issue={issue} />
            </div>
          </section>
        </div>
      </aside>
    </>
  );
}
