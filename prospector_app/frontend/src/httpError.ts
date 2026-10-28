/** A non-2xx answer from the backend, carrying its status so callers can tell
 *  "not in the store" (404) from a real failure. */
export class HttpError extends Error {
  readonly status: number;
  constructor(url: string, status: number) {
    super(`${url} → ${status}`);
    this.status = status;
  }
}

export function isNotFound(e: unknown): boolean {
  return e instanceof HttpError && e.status === 404;
}
