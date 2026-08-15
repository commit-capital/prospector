import { StrictMode, type ComponentType } from "react";
import { createRoot } from "react-dom/client";
import { createBrowserRouter, Navigate, useParams, useSearchParams } from "react-router";
import { RouterProvider } from "react-router/dom";
import "./styles.css";
import App from "./App";
import { LANES, type Lane } from "./components/explorer/lanes";

function lazyView(load: () => Promise<{ default: ComponentType }>) {
  return async () => ({ Component: (await load()).default });
}

// A direct /prs/:n link opens the flyout over the Explorer.
// eslint-disable-next-line react-refresh/only-export-components -- route redirect local to the entry's router setup
function PRRedirect() {
  const { n } = useParams();
  return <Navigate to={`/explore?pr=${n}`} replace />;
}

// /easy and /stale are lanes of the Explorer. Carry any existing query
// params through the redirect — notably ?pr=N, so a deep link like
// /easy?pr=6418 still opens that PR's flyout — and layer the lane's filter
// template on top.
// eslint-disable-next-line react-refresh/only-export-components -- route redirect local to the entry's router setup
function LaneRedirect({ lane }: { lane: Lane["key"] }) {
  const [sp] = useSearchParams();
  const next = new URLSearchParams(sp);
  next.set("spec", JSON.stringify(LANES.find((l) => l.key === lane)?.spec ?? {}));
  return <Navigate to={`/explore?${next}`} replace />;
}

const router = createBrowserRouter([
  {
    path: "/",
    element: <App />,
    children: [
      { index: true, lazy: lazyView(() => import("./views/Home")) },
      { path: "clusters", lazy: lazyView(() => import("./views/ClusterBoard")) },
      { path: "clusters/:id", lazy: lazyView(() => import("./views/ClusterDetail")) },
      { path: "explore", lazy: lazyView(() => import("./views/PRExplorer")) },
      { path: "differ", lazy: lazyView(() => import("./views/PRDiffer")) },
      { path: "prs", element: <Navigate to="/explore" replace /> },
      { path: "easy", element: <LaneRedirect lane="easy" /> },
      { path: "stale", element: <LaneRedirect lane="stale" /> },
      { path: "control", lazy: lazyView(() => import("./views/ControlPanel")) },
      { path: "action-items", lazy: lazyView(() => import("./views/ActionItems")) },
      { path: "issues", lazy: lazyView(() => import("./views/Issues")) },
      { path: "alerts", lazy: lazyView(() => import("./views/Alerts")) },
      { path: "activity", lazy: lazyView(() => import("./views/Activity")) },
      { path: "tables", lazy: lazyView(() => import("./views/Tables")) },
      { path: "tables/:name", lazy: lazyView(() => import("./views/TableDetail")) },
      { path: "prs/:n", element: <PRRedirect /> },
    ],
  },
]);

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <RouterProvider router={router} />
  </StrictMode>
);
