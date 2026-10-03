import type { ComponentType } from "react";

// The view each tab lands on, loaded on first use. The router's lazy routes
// (main.tsx) and the tab prefetch (prefetch.ts) share these loaders.
type ViewModule = Promise<{ default: ComponentType }>;

export const loadHome = (): ViewModule => import("./views/Home");
export const loadPRExplorer = (): ViewModule => import("./views/PRExplorer");
export const loadIssues = (): ViewModule => import("./views/Issues");
export const loadSecurity = (): ViewModule => import("./views/Alerts");
export const loadPipeline = (): ViewModule => import("./views/ControlPanel");

export const TAB_VIEWS: readonly (() => ViewModule)[] = [
  loadHome, loadPRExplorer, loadIssues, loadSecurity, loadPipeline,
];
