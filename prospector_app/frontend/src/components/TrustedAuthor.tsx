export const TRUSTED_AUTHOR_BADGE = "⭐";

export function TrustedAuthorName({
  author,
  trusted,
  fallback = "—",
}: {
  author: string | null | undefined;
  trusted?: boolean;
  fallback?: string;
}) {
  const handle = author ?? fallback;
  return <>{handle}{trusted ? `\u00A0${TRUSTED_AUTHOR_BADGE}` : ""}</>;
}
