import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";

// Web Speech API is not universally in TypeScript's DOM lib at all targets.
// Declare the minimal shape we need so we don't depend on the lib version.
interface SpeechRec extends EventTarget {
  continuous: boolean;
  interimResults: boolean;
  lang: string;
  onresult: ((e: SpeechRecognitionEvent) => void) | null;
  onerror: ((e: SpeechRecognitionErrorEvent) => void) | null;
  onend: (() => void) | null;
  start(): void;
  stop(): void;
}
interface SpeechRecCtor { new(): SpeechRec; }
declare global {
  interface Window {
    SpeechRecognition?: SpeechRecCtor;
    webkitSpeechRecognition?: SpeechRecCtor;
  }
}

const SR: SpeechRecCtor | null =
  typeof window !== "undefined"
    ? (window.SpeechRecognition ?? window.webkitSpeechRecognition ?? null)
    : null;

export function useSpeechInput(
  append: (text: string) => void,
  initialActive = false,
): { active: boolean; error: string; toggle: () => void; stop: () => void; supported: boolean } {
  const [active, setActive] = useState(initialActive);
  const [error, setError] = useState("");
  const recognitionRef = useRef<SpeechRec | null>(null);
  const appendRef = useRef(append);
  useLayoutEffect(() => { appendRef.current = append; });

  useEffect(() => {
    if (!SR || !active) return;

    const r = new SR();
    r.continuous = true;
    r.interimResults = false;
    r.lang = "en-US";
    r.onresult = (e: SpeechRecognitionEvent) => {
      const text = Array.from(e.results)
        .slice(e.resultIndex)
        .map((res) => res[0].transcript.trim())
        .filter(Boolean)
        .join(" ");
      if (text) appendRef.current(text);
    };
    r.onerror = (e: SpeechRecognitionErrorEvent) => {
      if (e.error !== "aborted") setError(e.error);
      setActive(false);
    };
    r.onend = () => {
      // Auto-restart to keep listening until the user toggles off
      if (recognitionRef.current === r) {
        try { r.start(); } catch { /* already started */ }
      }
    };
    r.start();
    recognitionRef.current = r;

    return () => {
      recognitionRef.current = null;
      try { r.stop(); } catch { /* already stopped */ }
    };
  }, [active]);

  const toggle = useCallback((): void => { setError(""); setActive((p) => !p); }, []);

  // Stop immediately (synchronous ref access) then update state — stable reference.
  const stop = useCallback((): void => {
    const r = recognitionRef.current;
    recognitionRef.current = null;
    try { r?.stop(); } catch { /* already stopped */ }
    setActive(false);
  }, []);

  return { active, error, toggle, stop, supported: SR !== null };
}
