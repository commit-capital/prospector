import { useEffect, useMemo, useState } from "react";
import { useSearchParams } from "react-router";
import { api, type PRRow } from "../api";
import { DiffGrid } from "../components/DiffGrid";
import { AddPrSearch } from "../components/AddPrSearch";

const PRS_PARAM = "prs";

function readPrs(params: URLSearchParams): number[] {
  return (params.get(PRS_PARAM) || "")
    .split(",").map((s) => Number(s.trim())).filter((n) => Number.isInteger(n) && n > 0);
}

/** Compare several PRs' diffs side by side as a file-aligned grid (see DiffGrid):
 *  every changed file lines up across the columns, blank where a PR doesn't touch
 *  it. The PR set lives in the URL (?prs=605,4836) so it's shareable and a
 *  cluster's compare action is just a link here. */
export default function PRDiffer() {
  const [params, setParams] = useSearchParams();
  const prs = useMemo(() => readPrs(params), [params]);
  const [rows, setRows] = useState<Record<number, PRRow>>({});

  // One cheap query for every column's header meta (title/author/url); the diff
  // itself is fetched per column inside DiffGrid.
  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- clear rows when there are no PRs selected
    if (!prs.length) { setRows({}); return; }
    api.queryPrs({ numbers: prs }, { limit: prs.length }).then((r) => {
      setRows(Object.fromEntries(r.items.map((it) => [it.number, it])));
    }).catch(() => {});
  }, [prs.join(",")]); // eslint-disable-line react-hooks/exhaustive-deps

  const setPrs = (next: number[]) => {
    const uniq = [...new Set(next)];
    const p = new URLSearchParams(params);
    if (uniq.length) p.set(PRS_PARAM, uniq.join(",")); else p.delete(PRS_PARAM);
    setParams(p);
  };
  const addPr = (n: number) => { if (n && !prs.includes(n)) setPrs([...prs, n]); };
  const removePr = (n: number) => setPrs(prs.filter((x) => x !== n));

  return (
    <div className="differ">
      <div className="differ-head">
        <h2>🔬 PR Differ</h2>
        <span className="muted small">{prs.length} PR{prs.length === 1 ? "" : "s"} · merge candidates on the left, closes on the right · the same file lines up across every column</span>
      </div>
      <div className="differ-bar">
        <AddPrSearch onAdd={addPr} exclude={prs} />
        {prs.length > 0 && <button className="link-btn" onClick={() => setPrs([])}>clear all</button>}
      </div>

      {prs.length === 0 ? (
        <div className="differ-empty">
          <p>Add PRs to compare their diffs side by side — type a number or search above,
            or use a cluster's <b>compare</b> action to open its PRs here.</p>
        </div>
      ) : (
        <div className="differ-scroll">
          <DiffGrid prs={prs} rows={rows} onRemove={removePr} groupByDisposition />
        </div>
      )}
    </div>
  );
}
