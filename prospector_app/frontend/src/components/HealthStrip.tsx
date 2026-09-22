import { Link } from "react-router";
import { useDeploymentHealth } from "../useDeploymentHealth";

// The thin cross-machine health line under the topbar, on every page: each
// problem names its machine and links to the page that fixes it. Renders
// nothing while the deployment is healthy.
export function HealthStrip() {
  const health = useDeploymentHealth();
  if (!health || health.items.length === 0) return null;
  return (
    <div className={`health-strip health-strip-${health.level}`} role="status">
      <span className="health-strip-icon" aria-hidden="true">
        {health.level === "red" ? "⛔" : "⚠"}
      </span>
      {health.items.map((it, i) => (
        <span key={it.key} className="health-strip-item">
          {i > 0 && <span className="health-strip-sep" aria-hidden="true">·</span>}
          <Link to={it.to} title={it.detail ?? undefined}>{it.text}</Link>
        </span>
      ))}
    </div>
  );
}
