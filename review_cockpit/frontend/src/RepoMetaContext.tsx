import { createContext, useContext, useEffect, useState, type ReactNode } from "react";
import { api, type RepoMeta } from "./api";

/** Backend-owned repository metadata, fetched once at bootstrap. `meta` is null
 *  until the fetch lands; the URL helpers return undefined then, which renders
 *  an <a> without an href rather than a link to the wrong repo. */
interface RepoMetaState {
  meta: RepoMeta | null;
  prUrl: (n: number) => string | undefined;
  issueUrl: (n: number) => string | undefined;
}

const Ctx = createContext<RepoMetaState>({
  meta: null, prUrl: () => undefined, issueUrl: () => undefined,
});

// eslint-disable-next-line react-refresh/only-export-components -- context hook co-located with its provider
export const useRepoMeta = () => useContext(Ctx);

export function RepoMetaProvider({ children }: { children: ReactNode }) {
  const [meta, setMeta] = useState<RepoMeta | null>(null);
  useEffect(() => {
    api.meta().then(setMeta).catch(() => {});
  }, []);
  const prUrl = (n: number) => (meta ? `${meta.url}/pull/${n}` : undefined);
  const issueUrl = (n: number) => (meta ? `${meta.url}/issues/${n}` : undefined);
  return <Ctx.Provider value={{ meta, prUrl, issueUrl }}>{children}</Ctx.Provider>;
}
