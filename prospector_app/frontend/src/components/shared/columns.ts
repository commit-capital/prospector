import type { ReactNode } from "react";
import type { ReviewerKind } from "../../api";

/** What the column chooser and its saved choices need of a table column. */
export interface ColumnMeta {
  key: string;
  label: string;            // header text + toggle-chip text
  defaultOn: boolean;       // visible when the user has no stored override
  fixed?: boolean;          // always shown, never in the toggle menu (anchors)
  capability?: ReviewerKind; // shown only while a reviewer of this kind is active
}

/** One column of an explorer table: how it is chosen, headed, and drawn for a
 *  `Row`, given the table's extra context `Ctx`. */
export interface Column<Row, Ctx> extends ColumnMeta {
  term?: string;            // glossary key → header InfoTip
  title?: string;           // header tooltip
  numeric?: boolean;        // right-aligned (the `num` class) on header + cell
  stopOpen?: boolean;       // cell swallows the row-open click (links)
  cellClass?: string;       // <td> className (e.g. "mono", "muted small")
  cell: (r: Row, ctx: Ctx) => ReactNode;
}
