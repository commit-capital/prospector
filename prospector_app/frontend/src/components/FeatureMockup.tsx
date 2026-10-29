import { useEffect, useState } from "react";
import { api } from "../api";
import { mockupSrcdoc } from "./mockupSrcdoc";

const FRAME_W = 1000;
const FRAME_H = 640;
const THUMB_SCALE = 0.25;

// A feature request's mockup page, in an iframe with no sandbox tokens (no
// script, no same-origin) under the CSP mockupSrcdoc adds. The thumbnail
// opens the full-size frame in a modal.
export function FeatureMockup({ issue, title }: { issue: number; title: string }) {
  const [html, setHtml] = useState<string | null>(null);
  const [failed, setFailed] = useState(false);
  const [open, setOpen] = useState(false);
  useEffect(() => {
    let cancelled = false;
    api.featureMockup(issue)
      .then((r) => { if (!cancelled) setHtml(r.html); })
      .catch(() => { if (!cancelled) setFailed(true); });
    return () => { cancelled = true; };
  }, [issue]);
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") setOpen(false); };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [open]);
  if (failed) return <div className="muted small">Mockup unavailable.</div>;
  if (html === null) return <div className="muted small">Loading mockup…</div>;
  const frame = (w: number, h: number, className: string) => (
    <iframe sandbox="" srcDoc={mockupSrcdoc(html)} title={`Mockup for ${title}`}
      className={className} style={{ width: w, height: h }} tabIndex={-1} />
  );
  return (
    <>
      <div className="feature-mockup-thumb"
        style={{ width: FRAME_W * THUMB_SCALE, height: FRAME_H * THUMB_SCALE }}>
        {frame(FRAME_W, FRAME_H, "feature-mockup-frame feature-mockup-frame-thumb")}
        <button type="button" className="feature-mockup-open" aria-label="Open the mockup full size"
          title="Open the mockup full size" onClick={() => setOpen(true)} />
      </div>
      {open && (
        <div className="modal-scrim" onClick={() => setOpen(false)}>
          <div className="modal feature-mockup-modal" role="dialog" aria-label={`Mockup for ${title}`}
            onClick={(e) => e.stopPropagation()}>
            <div className="feature-mockup-modal-head">
              <b>{title}</b>
              <button type="button" className="btn-secondary sm" onClick={() => setOpen(false)}>Close</button>
            </div>
            {frame(FRAME_W, FRAME_H, "feature-mockup-frame")}
          </div>
        </div>
      )}
    </>
  );
}
