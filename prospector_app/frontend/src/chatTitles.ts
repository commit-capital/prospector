/** Chat names summarized from each thread's first message, keyed by thread:
 *  a named session by its id, the current-page thread by its subject. */
export type ChatTitles = Record<string, string>;

export function titleKey(sessionId: string | null, subjectKey: string): string {
  return sessionId ?? `page:${subjectKey}`;
}

/** The name a thread shows: its summarized title, else its subject once it
 *  has messages, else "New chat". */
export function chatName(
  titles: ChatTitles, key: string, subjectLabel: string, hasConversation: boolean,
): string {
  return titles[key] || (hasConversation ? subjectLabel : "New chat");
}

export function parseTitles(raw: string | null): ChatTitles {
  if (raw === null) return {};
  try {
    const parsed: unknown = JSON.parse(raw);
    if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) return {};
    return Object.fromEntries(Object.entries(parsed)
      .filter((e): e is [string, string] => typeof e[1] === "string" && e[1] !== ""));
  } catch {
    return {};
  }
}
