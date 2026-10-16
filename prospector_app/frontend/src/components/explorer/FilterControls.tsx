import type { FilterSpec } from "../../api";
import { Term } from "../InfoTip";
import { term } from "../../glossary";
import { Select } from "../Select";

export function FilterControls({ spec, onChange }: {
  spec: FilterSpec;
  onChange: (next: FilterSpec) => void;
  // visibleColKeys kept in signature for backwards-compat with existing callers.
  visibleColKeys?: Set<string>;
}) {
  const set = (k: keyof FilterSpec, v: unknown) => {
    const next = { ...spec };
    if (v === "" || v === undefined) delete next[k];
    else (next as Record<string, unknown>)[k] = v;
    onChange(next);
  };

  return (
    <div className="chips">
      <span className="label"><Term k="ui.filters">Filters</Term></span>

      {/* draft: always shown — no dedicated column, lives as a chip in the Title column */}
      <Select value={spec.draft === undefined ? "" : spec.draft ? "yes" : "no"}
        onChange={(next) => set("draft", next === "" ? undefined : next === "yes")}
        title="Filter by whether the PR is a draft.">
        <option value="">draft: any</option>
        <option value="yes">draft only</option>
        <option value="no">non-draft only</option>
      </Select>

      <Select value={spec.stacked === undefined ? "" : spec.stacked ? "yes" : "no"}
        onChange={(next) => set("stacked", next === "" ? undefined : next === "yes")}
        title="Filter by whether the PR is stacked on another PR's branch rather than the default branch.">
        <option value="">stacked: any</option>
        <option value="yes">stacked only</option>
        <option value="no">not stacked</option>
      </Select>

      {/* state: always shown — the queue defaults to open PRs only */}
      <Select value={spec.state ?? ""}
        onChange={(next) => set("state", next || undefined)}
        title="Filter by PR state. Defaults to open PRs only.">
        <option value="">state: open only</option>
        <option value="closed">state: closed only</option>
        <option value="all">state: all (open + closed)</option>
      </Select>

      {/* paths: always shown — filters across all file paths, no dedicated table column */}
      <input className="author-in" placeholder="path contains" value={spec.paths ?? ""}
        title={term("filter.paths")?.meaning}
        onChange={(e) => set("paths", e.target.value)} />

      {/* responses: always shown — finds PRs where the author acted since we closed/actioned them */}
      <Select value={Array.isArray(spec.responses) ? "" : (spec.responses ?? "")}
        onChange={(next) => set("responses", next || undefined)}
        title="Filter by how the PR author responded since we acted on it. Includes closed PRs that were later reopened. Use the column header filter for multi-select.">
        <option value="">response: any</option>
        <option value="any">any response</option>
        <option value="reopened">↩ reopened</option>
        <option value="new_commits">⬆ new commits</option>
        <option value="replied">💬 replied</option>
        <option value="resubmitted">⤴ resubmitted</option>
      </Select>

      {/* author_return: the open PRs an author came back with after we closed their work */}
      <Select value={spec.author_return === true ? "any" : Array.isArray(spec.author_return) ? "" : (spec.author_return ?? "")}
        onChange={(next) => set("author_return", next === "any" ? true : (next || undefined))}
        title="Open PRs whose author came back after we closed their work: reopened it, or opened this PR linking the PR or issue we closed.">
        <option value="">came back: any</option>
        <option value="any">author came back</option>
        <option value="reopened">↩ reopened</option>
        <option value="follow-up">↪ follow-up to our close</option>
      </Select>

      <button className="link-btn" onClick={() => onChange({})}>Clear all</button>
    </div>
  );
}
