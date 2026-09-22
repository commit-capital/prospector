/** Untrusted upstream text (advisory summaries, alert titles) is markdown
 *  rendered here as plain text, so a leading heading marker would show as a
 *  literal "#". Drop the marker and keep the words. */
export function stripMdHeading(text: string | null | undefined): string | null {
  if (text == null) return null;
  return text.replace(/^\s*#{1,6}\s+/, "");
}
