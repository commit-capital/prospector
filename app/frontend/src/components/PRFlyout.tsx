import { useEffect, useState } from "react";
import { usePRFlyout } from "../usePRFlyout";
import { useResizableWidth } from "../useResizableWidth";
import { PRDetailContent } from "../views/PRDetail";

/** Right-side flyout that shows PR details over whatever page you're on.
 *  Open via ?pr=N (set by PRLink / row clicks). Ctrl-click a row stacks an
 *  extra pane (?pr=N,M) so two or three PRs can be compared top-to-bottom (#20).
 *  Drag the left edge to resize the column; drag a pane's bottom edge to
 *  resize that pane. ESC or ✕ closes. */
export function PRFlyout() {
  const { prs, closePane, close } = usePRFlyout();
  const [maximized, setMaximized] = useState(false);
  const { width, startResize } = useResizableWidth("app-flyout-width", 640);
  // explicit per-pane heights (px) once the user drags a pane divider
  const [heights, setHeights] = useState<Record<number, number>>({});

  useEffect(() => {
    if (!prs.length) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") close(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [prs.length, close]);

  if (!prs.length) return null;
  const stacked = prs.length > 1;

  const startPaneResize = (pr: number, startY: number, startH: number) => {
    const onMove = (e: MouseEvent) =>
      setHeights((h) => ({ ...h, [pr]: Math.max(160, startH + (e.clientY - startY)) }));
    const onUp = () => {
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup", onUp);
    };
    document.body.style.cursor = "row-resize";
    document.body.style.userSelect = "none";
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);
  };

  return (
    <>
      <div className="flyout-scrim" onClick={close} />
      <aside className={`flyout ${maximized ? "flyout-max" : ""} ${stacked ? "flyout-stacked" : ""}`}
        role="dialog" aria-label={prs.length === 1 ? `PR ${prs[0]}` : `${prs.length} PRs`}
        style={maximized ? undefined : { width }}>
        <div className="flyout-resize" onMouseDown={startResize} title="Drag to resize" role="separator" aria-orientation="vertical" />
        <div className="flyout-bar">
          {stacked && <span className="flyout-count">{prs.length} PRs stacked</span>}
          <button className="flyout-btn" title={maximized ? "Restore" : "Maximize"} onClick={() => setMaximized((m) => !m)}>
            {maximized ? "⤡" : "⤢"}
          </button>
          {!stacked && (
            <a className="flyout-btn" title="Open full page" href={`/prs/${prs[0]}`} target="_blank" rel="noreferrer">↗</a>
          )}
          <button className="flyout-btn flyout-close" title="Close all (Esc)" onClick={close}>✕</button>
        </div>
        <div className="flyout-panes">
          {prs.map((pr, i) => (
            <section className="flyout-pane" key={pr}
              style={stacked && heights[pr] ? { height: heights[pr], flex: "none" } : undefined}>
              {stacked && (
                <div className="pane-head">
                  <a className="pane-title" href={`/prs/${pr}`} target="_blank" rel="noreferrer" title="Open full page">PR #{pr} ↗</a>
                  <button className="flyout-btn flyout-close pane-close" title="Close this pane" onClick={() => closePane(pr)}>✕</button>
                </div>
              )}
              <div className="flyout-body">
                <PRDetailContent key={pr} pr={pr} />
              </div>
              {stacked && i < prs.length - 1 && (
                <div className="pane-resize" title="Drag to resize this pane" role="separator" aria-orientation="horizontal"
                  onMouseDown={(e) => {
                    const el = (e.currentTarget.parentElement as HTMLElement);
                    startPaneResize(pr, e.clientY, el.getBoundingClientRect().height);
                  }} />
              )}
            </section>
          ))}
        </div>
      </aside>
    </>
  );
}
