// A browser opens at most six HTTP/1.1 connections to one origin, and a job's
// or the agent's event stream holds one for as long as it runs. Reads share
// READ_SLOTS of those connections and wait their turn past that, so a stream,
// an action, or an urgent read always finds a connection free however slowly
// the backend answers.
export const READ_SLOTS = 3;

let inFlight = 0;
const waiting: (() => void)[] = [];

/** Run `read` once a read slot is free, holding the slot until it settles. */
export async function withReadSlot<T>(read: () => Promise<T>): Promise<T> {
  if (inFlight < READ_SLOTS) inFlight += 1;
  else await new Promise<void>((resume) => { waiting.push(resume); });
  try {
    return await read();
  } finally {
    // A settled read hands its slot straight to the next one waiting.
    const next = waiting.shift();
    if (next) next();
    else inFlight -= 1;
  }
}
