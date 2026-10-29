/** The policy a feature mockup runs under: it may style itself and inline
 *  data: images and fonts, and fetch nothing. */
export const MOCKUP_CSP = "default-src 'none'; style-src 'unsafe-inline'; img-src data:; font-src data:";

const CSP_META = `<meta http-equiv="Content-Security-Policy" content="${MOCKUP_CSP}">`;
const LEADING_DOCTYPE = /^\s*<!doctype[^>]*>/i;

/** The mockup page behind the CSP meta: the meta is the first element the
 *  parser sees, in the implied head, ahead of any mockup markup. */
export function mockupSrcdoc(html: string): string {
  return `<!doctype html>${CSP_META}${html.replace(LEADING_DOCTYPE, "")}`;
}
