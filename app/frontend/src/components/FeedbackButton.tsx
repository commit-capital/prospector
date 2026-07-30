import { useEffect, useState } from "react";
import { useLocation } from "react-router";
import { api, type FeedbackTarget } from "../api";
import { buildIssueUrl } from "../feedbackUrl";
import { useSpeechInput } from "../useSpeechInput";

function MicIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
      <path d="M12 14c1.66 0 3-1.34 3-3V5c0-1.66-1.34-3-3-3S9 3.34 9 5v6c0 1.66 1.34 3 3 3zm5.3-3c0 3-2.54 5.1-5.3 5.1S6.7 14 6.7 11H5c0 3.41 2.72 6.23 6 6.72V21h2v-3.28c3.28-.48 6-3.3 6-6.72h-1.7z"/>
    </svg>
  );
}

export function FeedbackButton() {
  const [open, setOpen] = useState(false);
  const [target, setTarget] = useState<FeedbackTarget | null>(null);
  const { pathname, search, hash } = useLocation();
  const pageUrl = `${window.location.origin}${pathname}${search}${hash}`;

  useEffect(() => {
    api.feedbackTarget().then(setTarget).catch(() => {});
  }, []);

  // No configured feedback repo (PROSPECTOR_FEEDBACK_REPO unset) → nowhere to file,
  // so the button doesn't render.
  if (!target?.repo) return null;

  return (
    <>
      <button
        className="feedback-btn"
        onClick={() => setOpen(true)}
        title="Report a bug or request a feature for the cockpit"
      >
        🐞 Feedback
      </button>
      {open && (
        <FeedbackModal target={target} pageUrl={pageUrl} onClose={() => setOpen(false)} />
      )}
    </>
  );
}

function FeedbackModal({
  target,
  pageUrl,
  onClose,
}: {
  target: FeedbackTarget;
  pageUrl: string;
  onClose: () => void;
}) {
  const [description, setDescription] = useState("");
  const [loading, setLoading] = useState(false);
  const [loadingPhase, setLoadingPhase] = useState<"screenshot" | "generating" | null>(null);
  const [clipOk, setClipOk] = useState<boolean | null>(null);
  const [screenshotPreview, setScreenshotPreview] = useState<string | null>(null);
  const [screenshot, setScreenshot] = useState(
    () => localStorage.getItem("feedback-screenshot") !== "false",
  );
  const speech = useSpeechInput(
    (text) => setDescription((prev) => (prev ? prev + " " + text : text)),
    localStorage.getItem("feedback-mic") === "true",
  );

  // Persist mic preference whenever it changes (covers toggle and auto-stop on error)
  useEffect(() => {
    localStorage.setItem("feedback-mic", String(speech.active));
  }, [speech.active]);

  const toggleMic = () => { speech.toggle(); };

  const toggleScreenshot = () => {
    const next = !screenshot;
    setScreenshot(next);
    localStorage.setItem("feedback-screenshot", String(next));
  };

  const openIssue = async () => {
    if (!description.trim() || loading) return;

    // Open the tab synchronously, inside the click gesture, so the popup blocker
    // allows it; redirect it to the prefilled issue URL once title/body
    // generation finishes.
    const issueTab = window.open("", "_blank");
    if (issueTab) issueTab.opener = null;

    setLoading(true);
    setClipOk(null);
    setScreenshotPreview(null);

    // Stop mic so it doesn't capture noise during async work
    speech.stop();

    // Capture screenshot before the page changes
    let clipboardSucceeded = false;
    if (screenshot) {
      setLoadingPhase("screenshot");
      try {
        const { default: html2canvas } = await import("html2canvas-pro");
        const canvas = await html2canvas(document.documentElement, {
          scale: 1,
          useCORS: true,
          logging: false,
          allowTaint: true,
          // Capture the app as it looked before the feedback dialog opened, not
          // the dialog itself sitting on top of it.
          ignoreElements: (el) => el.classList.contains("feedback-overlay"),
        });
        const blob = await new Promise<Blob | null>((resolve) => {
          canvas.toBlob((b) => resolve(b), "image/png");
        });
        if (blob) {
          try {
            await navigator.clipboard.write([new ClipboardItem({ "image/png": blob })]);
            setClipOk(true);
            clipboardSucceeded = true;
          } catch {
            // clipboard.write() failed (permission denied, non-HTTPS, etc.)
            // Fall back: store a data URL so the user can right-click-copy
            setClipOk(false);
            try { setScreenshotPreview(canvas.toDataURL("image/png")); } catch { /* ok */ }
          }
        } else {
          setClipOk(false);
        }
      } catch {
        setClipOk(false);
      }
    }

    // Generate a polished title + body from the description
    setLoadingPhase("generating");
    let title = "";
    let body = description;
    try {
      const result = await api.generateFeedback(description.trim());
      title = result.title || "";
      body = result.body || description;
    } catch { /* fallback: use raw description as body */ }

    const url = buildIssueUrl(target, pageUrl, title || undefined, body);
    if (!url) {
      issueTab?.close();
    } else if (issueTab) {
      issueTab.location.href = url;
    } else {
      window.open(url, "_blank", "noopener,noreferrer");
    }
    setLoading(false);
    setLoadingPhase(null);

    // Auto-close only when screenshot wasn't requested or it was copied successfully.
    // If clipboard write failed, stay open so the user can see and copy the preview.
    if (!screenshot || clipboardSucceeded) {
      onClose();
    }
  };

  const handleOverlayClick = (e: React.MouseEvent<HTMLDivElement>) => {
    if (e.target === e.currentTarget) onClose();
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Escape") onClose();
    if ((e.metaKey || e.ctrlKey) && e.key === "Enter") void openIssue();
  };

  return (
    <div className="feedback-overlay" onClick={handleOverlayClick} onKeyDown={handleKeyDown}>
      <div className="feedback-modal" role="dialog" aria-modal="true" aria-label="File a bug or feature request">
        <div className="feedback-modal-header">
          <span className="feedback-modal-title">🐞 File a bug or request</span>
          <button className="feedback-close" onClick={onClose} aria-label="Close">✕</button>
        </div>

        <div className="feedback-modal-body">
          <div className="feedback-textarea-wrap">
            <textarea
              className="feedback-textarea"
              placeholder="Describe the bug or feature request… Claude will generate a title and format your report before opening the issue."
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              rows={6}
              autoFocus
              disabled={loading}
            />
            {speech.supported && (
              <button
                className={`feedback-mic-btn${speech.active ? " active" : ""}`}
                onClick={toggleMic}
                title={speech.active ? "Stop voice input" : "Start voice input (speech to text)"}
                aria-label={speech.active ? "Stop voice input" : "Start voice input"}
                disabled={loading}
              >
                <MicIcon />
              </button>
            )}
          </div>
          {speech.error && (
            <div className="feedback-mic-error">
              Mic error: {speech.error}. Check browser permissions.
            </div>
          )}

          <label className="feedback-option">
            <input
              type="checkbox"
              checked={screenshot}
              onChange={toggleScreenshot}
              disabled={loading}
            />
            <span>Copy screenshot to clipboard when opening issue</span>
          </label>

          {screenshotPreview && (
            <div className="feedback-screenshot-fallback">
              <span className="feedback-screenshot-fallback-label">
                ⚠️ Couldn't copy to clipboard — right-click the image below to copy it manually:
              </span>
              <img
                src={screenshotPreview}
                alt="Page screenshot"
                className="feedback-screenshot-preview"
              />
              <button
                className="feedback-cancel"
                onClick={onClose}
              >
                Done
              </button>
            </div>
          )}
        </div>

        <div className="feedback-modal-footer">
          <span className="feedback-hint">
            {loading && loadingPhase === "screenshot"
              ? "Taking screenshot…"
              : loading
              ? "Generating issue…"
              : clipOk === true
              ? "📸 Screenshot copied to clipboard"
              : clipOk === false && !screenshotPreview
              ? "⚠️ Screenshot copy failed"
              : "⌘↵ to open"}
          </span>
          {!screenshotPreview && (
            <div className="feedback-actions">
              <button className="feedback-cancel" onClick={onClose} disabled={loading}>
                Cancel
              </button>
              <button
                className="feedback-submit"
                onClick={openIssue}
                disabled={!description.trim() || loading}
              >
                {loading ? "Opening…" : "Open Issue →"}
              </button>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
