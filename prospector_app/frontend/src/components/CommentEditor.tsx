// The "📮 <bot> will post … ▾" collapsible comment editor shared by the action
// bars: a toggle with a peek line, a textarea, and a reset-to-default
// affordance. The parent owns the text and edited-flag state.
export function CommentEditor({
  botLogin, value, isEdited, open, onToggle, onChange, onReset, placeholder,
}: {
  botLogin: string; value: string; isEdited: boolean; open: boolean;
  onToggle: () => void; onChange: (v: string) => void; onReset: () => void; placeholder: string;
}) {
  return (
    <div className="sug-comment pr-actions-comment">
      <button className="sug-comment-toggle" onClick={onToggle}>
        📮 {botLogin} will post {isEdited && <span className="sug-edited" title="You edited this from the default wording">✎ edited</span>} {open ? "▾" : "▸"}
        {!open && <span className="sug-comment-peek"> {value.replace(/\s+/g, " ").slice(0, 64) || "(empty — click to write)"}…</span>}
      </button>
      {open && (
        <div className="sug-comment-edit-box">
          <textarea className="sug-comment-textarea" value={value}
            rows={Math.min(12, value.split("\n").length + 1)}
            placeholder={placeholder}
            onChange={(e) => onChange(e.target.value)}
            aria-label={`Comment ${botLogin} will post`} />
          <div className="sug-comment-actions">
            <button className="btn-secondary sm" disabled={!isEdited} onClick={onReset}
              title="Restore the default wording">↺ Reset to default</button>
            {isEdited && <span className="muted small">posting your edited text, not the default</span>}
          </div>
        </div>
      )}
    </div>
  );
}
