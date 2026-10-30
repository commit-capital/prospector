import { startColumnResize, widthCss } from "../../columnLayout";

/** A header's hide button (shown on hover) and its right-edge resize handle;
 *  double-clicking the handle returns the column to its natural width. */
export function ColumnHeaderTools({ label, onHide, onWidth }: {
  label: string;
  onHide?: () => void;
  onWidth: (w: number | null) => void;
}) {
  return (
    <>
      {onHide && (
        <button className="col-hide-btn" title={`Hide ${label}`} aria-label={`Hide ${label} column`}
          onClick={(e) => { e.stopPropagation(); onHide(); }}>×</button>
      )}
      <span className="col-resize" role="separator" aria-orientation="vertical"
        title="Drag to resize · double-click to fit"
        onMouseDown={(e) => startColumnResize(e, onWidth)}
        onClick={(e) => e.stopPropagation()}
        onDoubleClick={(e) => { e.stopPropagation(); onWidth(null); }} />
    </>
  );
}

/** The `<style>` pinning `tableClass`'s resized columns to their widths. */
export function ColumnWidthStyle({ tableClass, widths }: { tableClass: string; widths: Record<string, number> }) {
  return <style>{widthCss(tableClass, widths)}</style>;
}
