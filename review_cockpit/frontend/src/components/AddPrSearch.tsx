import { useEffect, useRef, useState } from "react";
import { api, type PRRow } from "../api";

/** Type a PR number or search by title; pick a result to add it. Used by the PR
 *  Differ tab and the cluster page's embedded comparison — the latter to pull in
 *  a PR that isn't a cluster member (e.g. a duplicate's canonical target). */
export function AddPrSearch({ onAdd, exclude, placeholder = "add a PR — number or title…" }: {
  onAdd: (n: number) => void;
  exclude: number[];
  placeholder?: string;
}) {
  const [text, setText] = useState("");
  const [hits, setHits] = useState<PRRow[]>([]);
  const [open, setOpen] = useState(false);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    if (timer.current) clearTimeout(timer.current);
    const q = text.trim();
    if (!q) return; // nothing to search; the menu is hidden on empty text below
    timer.current = setTimeout(() => {
      api.queryPrs({ q }, { limit: 6 }).then((r) =>
        setHits(r.items.filter((it) => !exclude.includes(it.number)))).catch(() => setHits([]));
    }, 200);
    return () => { if (timer.current) clearTimeout(timer.current); };
  }, [text, exclude]);

  const add = (n: number) => { onAdd(n); setText(""); setHits([]); setOpen(false); };
  const num = Number(text.trim());
  const bareNumber = Number.isInteger(num) && num > 0 && !exclude.includes(num);

  return (
    <div className="addpr">
      <input className="addpr-input" value={text} placeholder={placeholder}
        onChange={(e) => { setText(e.target.value); setOpen(true); }}
        onFocus={() => setOpen(true)}
        onBlur={() => setTimeout(() => setOpen(false), 150)}
        onKeyDown={(e) => { if (e.key === "Enter" && bareNumber) add(num); }} />
      {open && text.trim() && (hits.length > 0 || bareNumber) && (
        <div className="addpr-menu">
          {bareNumber && <button className="addpr-opt" onMouseDown={(e) => e.preventDefault()} onClick={() => add(num)}>+ Add PR #{num}</button>}
          {hits.map((h) => (
            <button key={h.number} className="addpr-opt" onMouseDown={(e) => e.preventDefault()} onClick={() => add(h.number)}>
              <span className="mono">#{h.number}</span> <span className="addpr-opt-title">{h.title}</span>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
