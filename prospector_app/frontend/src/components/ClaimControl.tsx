import { useEffect, useState } from "react";
import { api, type ItemClaim } from "../api";
import { timeAgo } from "../timeAgo";

/** Claimed-by marker plus claim/release control for one PR or issue. The claim
 *  lives in the store's shared registry, so every operator's app shows the same
 *  marker and two people don't work the same item unknowingly. */
export function ClaimControl({ kind, n }: { kind: "pr" | "issue"; n: number }) {
  const [claim, setClaim] = useState<ItemClaim | null | undefined>(undefined);
  const [meBy, setMeBy] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let live = true;
    api.claims()
      .then((r) => { if (live) { setClaim(r.items[`${kind}:${n}`] ?? null); setMeBy(r.me.by); } })
      .catch(() => { if (live) setClaim(undefined); });
    return () => { live = false; };
  }, [kind, n]);

  if (claim === undefined) return null;
  const mine = claim !== null && claim.by === meBy;

  const set = (action: "claim" | "release") => {
    if (action === "claim" && claim && !mine
        && !window.confirm(`${claim.by} claimed this ${timeAgo(claim.at)} ago (on ${claim.machine}). Take it over?`)) return;
    setBusy(true);
    api.setClaim(kind, n, action)
      .then((r) => { setClaim(r.items[`${kind}:${n}`] ?? null); setMeBy(r.me.by); })
      .catch(() => {})
      .finally(() => setBusy(false));
  };

  return (
    <>
      {claim && (
        <span className={`chip sm ${mine ? "chip-green" : "chip-gold"}`}
          title={`Claimed by ${claim.by} on ${claim.machine}, ${timeAgo(claim.at)} ago — shared across operators so two people don't work the same item.`}>
          {mine ? "claimed by you" : `claimed · ${claim.by}`}
        </span>
      )}
      <button className="linkish small" disabled={busy}
        title={claim
          ? (mine ? "Release your claim on this item." : `Take over the claim from ${claim.by}.`)
          : "Mark this item as yours — teammates see the claim on their Home and flyouts."}
        onClick={() => set(mine ? "release" : "claim")}>
        {busy ? "…" : claim ? (mine ? "release" : "take over") : "claim"}
      </button>
    </>
  );
}
