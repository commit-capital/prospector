import type { PRRow, QueryResult } from "../../api";

// Each fetch parses fresh row objects, so a row's identity changes even when
// its content didn't — and identity is what React.memo compares. Fingerprints
// are cached per object (WeakMap), so a row carried across many refetches is
// serialized once, not once per refetch.
const fingerprints = new WeakMap<PRRow, string>();

function fingerprint(r: PRRow): string {
  let s = fingerprints.get(r);
  if (s === undefined) {
    s = JSON.stringify(r);
    fingerprints.set(r, s);
  }
  return s;
}

/** Carry content-identical row objects from `prev` over into `next`'s items,
 *  so a refetch that changes nothing about a PR keeps that row's object
 *  identity and its memoized `<PrRow>` skips re-rendering. Rows are matched by
 *  PR number and compared by their JSON serialization; the server builds each
 *  row's keys in one code path, so equal content serializes equally. */
export function reuseRows(prev: QueryResult | null, next: QueryResult): QueryResult {
  if (!prev || prev.items.length === 0) return next;
  const prevByNumber = new Map(prev.items.map((r) => [r.number, r]));
  const items = next.items.map((r) => {
    const old = prevByNumber.get(r.number);
    return old !== undefined && fingerprint(old) === fingerprint(r) ? old : r;
  });
  return { ...next, items };
}
