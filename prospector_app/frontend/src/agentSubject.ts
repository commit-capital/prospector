export type AgentAlertSource = "code-scanning" | "dependabot" | "secret-scanning";

interface SubjectBase {
  key: string;
  label: string;
}

export type AgentSubject =
  | (SubjectBase & { kind: "pr"; number: number })
  | (SubjectBase & { kind: "cluster"; number: number })
  | (SubjectBase & { kind: "issue"; number: number })
  | (SubjectBase & { kind: "alert"; source: AgentAlertSource; number: number })
  | (SubjectBase & { kind: "advisory"; ghsa: string })
  | (SubjectBase & { kind: "general" });

const GENERAL_SUBJECT: AgentSubject = {
  kind: "general", key: "general", label: "this app or codebase",
};

function positiveInteger(value: string | null): number | null {
  if (value === null || !/^\d+$/.test(value)) return null;
  const number = Number(value);
  return number > 0 ? number : null;
}

function alertSource(value: string | null): AgentAlertSource | null {
  switch (value) {
    case "code-scanning":
    case "dependabot":
    case "secret-scanning":
      return value;
    default:
      return null;
  }
}

export function subjectFromLocation(pathname: string, search: string): AgentSubject {
  const params = new URLSearchParams(search);
  const pr = positiveInteger(params.get("pr"));
  if (pr !== null) return { kind: "pr", key: `pr:${pr}`, number: pr, label: `PR #${pr}` };

  const issue = positiveInteger(params.get("issue"));
  if (issue !== null) {
    return { kind: "issue", key: `issue:${issue}`, number: issue, label: `issue #${issue}` };
  }

  const clusterMatch = pathname.match(/^\/clusters\/(\d+)/);
  const cluster = positiveInteger(clusterMatch?.[1] ?? null);
  if (cluster !== null) {
    return {
      kind: "cluster", key: `cluster:${cluster}`, number: cluster, label: `cluster ${cluster}`,
    };
  }

  if (pathname !== "/alerts") return GENERAL_SUBJECT;

  const ghsa = params.get("advisory");
  if (ghsa) return { kind: "advisory", key: `advisory:${ghsa}`, ghsa, label: `advisory ${ghsa}` };

  const source = alertSource(params.get("alert_source"));
  const alert = positiveInteger(params.get("alert"));
  if (source !== null && alert !== null) {
    return {
      kind: "alert", key: `alert:${source}:${alert}`, source, number: alert,
      label: `${source} alert #${alert}`,
    };
  }

  return GENERAL_SUBJECT;
}

export function addSubjectParams(params: URLSearchParams, subject: AgentSubject): void {
  switch (subject.kind) {
    case "pr":
      params.set("pr", String(subject.number));
      break;
    case "cluster":
      params.set("cluster", String(subject.number));
      break;
    case "issue":
      params.set("issue", String(subject.number));
      break;
    case "advisory":
      params.set("advisory", subject.ghsa);
      break;
    case "alert":
      params.set("alert_source", subject.source);
      params.set("alert", String(subject.number));
      break;
    case "general":
      break;
  }
}
