import { StrictMode, type ComponentType } from "react";
import { createRoot } from "react-dom/client";
import { createBrowserRouter, Navigate, useLocation, useParams, useSearchParams } from "react-router";
import { RouterProvider } from "react-router/dom";
import "./styles.css";
import App from "./App";
import { LANES, type Lane } from "./components/explorer/lanes";

function lazyView(load: () => Promise<{ default: ComponentType }>) {
  return async () => ({ Component: (await load()).default });
}

// A direct /prs/:n link opens the flyout over the PR list.
// eslint-disable-next-line react-refresh/only-export-components -- route redirect local to the entry's router setup
function PRRedirect() {
  const { n } = useParams();
  return <Navigate to={`/prs/list?pr=${n}`} replace />;
}

// /easy and /stale are lanes of the PR list. Carry any existing query
// params through the redirect — notably ?pr=N, so a deep link like
// /easy?pr=6418 still opens that PR's flyout — and layer the lane's filter
// template on top.
// eslint-disable-next-line react-refresh/only-export-components -- route redirect local to the entry's router setup
function LaneRedirect({ lane }: { lane: Lane["key"] }) {
  const [sp] = useSearchParams();
  const next = new URLSearchParams(sp);
  next.set("spec", JSON.stringify(LANES.find((l) => l.key === lane)?.spec ?? {}));
  return <Navigate to={`/prs/list?${next}`} replace />;
}

// A view's old route, redirected to its home under the five destinations with
// the query string (flyout params, filter specs) carried through.
// eslint-disable-next-line react-refresh/only-export-components -- route redirect local to the entry's router setup
function Moved({ to }: { to: string }) {
  const { search } = useLocation();
  return <Navigate to={`${to}${search}`} replace />;
}

// eslint-disable-next-line react-refresh/only-export-components -- route redirect local to the entry's router setup
function ClusterMoved() {
  const { id } = useParams();
  const { search } = useLocation();
  return <Navigate to={`/prs/clusters/${id}${search}`} replace />;
}

// eslint-disable-next-line react-refresh/only-export-components -- route redirect local to the entry's router setup
function TableMoved() {
  const { name } = useParams();
  const { search } = useLocation();
  return <Navigate to={`/pipeline/data/${name}${search}`} replace />;
}

const router = createBrowserRouter([
  {
    path: "/",
    element: <App />,
    children: [
      // The five destinations: Home, PRs, Issues, Security, Pipeline.
      { index: true, lazy: lazyView(() => import("./views/Home")) },
      { path: "prs", element: <Navigate to="/prs/list" replace /> },
      { path: "prs/list", lazy: lazyView(() => import("./views/PRExplorer")) },
      { path: "prs/clusters", lazy: lazyView(() => import("./views/ClusterBoard")) },
      { path: "prs/clusters/:id", lazy: lazyView(() => import("./views/ClusterDetail")) },
      { path: "prs/compare", lazy: lazyView(() => import("./views/PRDiffer")) },
      { path: "issues", lazy: lazyView(() => import("./views/Issues")) },
      { path: "security", lazy: lazyView(() => import("./views/Alerts")) },
      { path: "security/actions", lazy: lazyView(() => import("./views/ActionItems")) },
      { path: "pipeline", element: <Navigate to="/pipeline/control" replace /> },
      { path: "pipeline/control", lazy: lazyView(() => import("./views/ControlPanel")) },
      { path: "pipeline/activity", lazy: lazyView(() => import("./views/Activity")) },
      { path: "pipeline/policy", lazy: lazyView(() => import("./views/Policy")) },
      { path: "pipeline/setup", lazy: lazyView(() => import("./views/Setup")) },
      { path: "pipeline/data", lazy: lazyView(() => import("./views/Tables")) },
      { path: "pipeline/data/:name", lazy: lazyView(() => import("./views/TableDetail")) },
      { path: "welcome", lazy: lazyView(() => import("./views/Welcome")) },
      // Old routes redirect to their new homes, query string included.
      { path: "explore", element: <Moved to="/prs/list" /> },
      { path: "clusters", element: <Moved to="/prs/clusters" /> },
      { path: "clusters/:id", element: <ClusterMoved /> },
      { path: "differ", element: <Moved to="/prs/compare" /> },
      { path: "alerts", element: <Moved to="/security" /> },
      { path: "action-items", element: <Moved to="/security/actions" /> },
      { path: "control", element: <Moved to="/pipeline/control" /> },
      { path: "activity", element: <Moved to="/pipeline/activity" /> },
      { path: "setup", element: <Moved to="/pipeline/setup" /> },
      { path: "tables", element: <Moved to="/pipeline/data" /> },
      { path: "tables/:name", element: <TableMoved /> },
      { path: "easy", element: <LaneRedirect lane="easy" /> },
      { path: "stale", element: <LaneRedirect lane="stale" /> },
      { path: "prs/:n", element: <PRRedirect /> },
    ],
  },
]);

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <RouterProvider router={router} />
  </StrictMode>
);
