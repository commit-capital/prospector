// The latest answer to each read a page can paint before its own read lands —
// a tab's first screen, read ahead while another tab is showing (prefetch.ts).
// The page shows the answer at once and reads again, so an answer here is only
// ever on screen until the page's own read replaces it.

export const MAX_ANSWERS = 50;

export type Read<T> = { key: string; load: () => Promise<T> };

const answers = new Map<string, unknown>();

export function defineRead<T>(key: string, load: () => Promise<T>): Read<T> {
  return { key, load };
}

/** The latest answer a read under `read.key` received, if one has landed. */
export function peekRead<T>(read: Read<T>): T | undefined {
  return answers.get(read.key) as T | undefined;
}

/** Run `read`, keeping its answer for `peekRead`; past MAX_ANSWERS the least
 *  recently answered key is dropped. */
export async function runRead<T>(read: Read<T>): Promise<T> {
  const answer = await read.load();
  answers.delete(read.key);
  answers.set(read.key, answer);
  if (answers.size > MAX_ANSWERS) {
    const oldest = answers.keys().next();
    if (!oldest.done) answers.delete(oldest.value);
  }
  return answer;
}
