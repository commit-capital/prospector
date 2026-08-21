import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from "react";
import { api, type RepoMeta } from "./api";

/** Backend-owned repository metadata, fetched at bootstrap and retried until it
 *  lands. `meta` is null until then — the shell renders no tabs and no page
 *  while it is, since which page to show depends on it — and the URL helpers
 *  return undefined, which renders an <a> without an href rather than a link
 *  to the wrong repo. */
interface RepoMetaState {
  meta: RepoMeta | null;
  /** Re-read it. The wizard configures the deployment while the app is
   *  running, which changes the repository this whole shell is named after. */
  refresh: () => void;
  prUrl: (n: number) => string | undefined;
  issueUrl: (n: number) => string | undefined;
}

const Ctx = createContext<RepoMetaState>({
  meta: null, refresh: () => {}, prUrl: () => undefined, issueUrl: () => undefined,
});

// eslint-disable-next-line react-refresh/only-export-components -- context hook co-located with its provider
export const useRepoMeta = () => useContext(Ctx);

export function RepoMetaProvider({ children }: { children: ReactNode }) {
  const [meta, setMeta] = useState<RepoMeta | null>(null);
  // Each refresh restarts the read below; a failed read (backend still
  // starting, or down) is retried every few seconds until one lands.
  const [generation, setGeneration] = useState(0);
  const refresh = useCallback(() => setGeneration((g) => g + 1), []);
  useEffect(() => {
    let live = true;
    let timer: number | undefined;
    const load = () => {
      api.meta()
        .then((m) => { if (live) setMeta(m); })
        .catch(() => { if (live) timer = window.setTimeout(load, 3000); });
    };
    timer = window.setTimeout(load, 0);
    return () => { live = false; window.clearTimeout(timer); };
  }, [generation]);
  const prUrl = (n: number) => (meta ? `${meta.url}/pull/${n}` : undefined);
  const issueUrl = (n: number) => (meta ? `${meta.url}/issues/${n}` : undefined);
  return <Ctx.Provider value={{ meta, refresh, prUrl, issueUrl }}>{children}</Ctx.Provider>;
}
