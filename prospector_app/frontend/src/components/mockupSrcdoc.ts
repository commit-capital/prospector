/** The policy a feature mockup runs under: it may style itself and inline
 *  data: images and fonts, and fetch nothing. */
export const MOCKUP_CSP = "default-src 'none'; style-src 'unsafe-inline'; img-src data:; font-src data:";

const CSP_META = `<meta http-equiv="Content-Security-Policy" content="${MOCKUP_CSP}">`;

/** The mockup page with the CSP meta as the first element of its head, so no
 *  mockup markup is parsed before the policy applies. */
export function mockupSrcdoc(html: string): string {
  const head = /<head(\s[^>]*)?>/i.exec(html);
  if (head) return html.slice(0, head.index + head[0].length) + CSP_META + html.slice(head.index + head[0].length);
  const root = /<html(\s[^>]*)?>/i.exec(html);
  if (root) {
    const end = root.index + root[0].length;
    return `${html.slice(0, end)}<head>${CSP_META}</head>${html.slice(end)}`;
  }
  return `<head>${CSP_META}</head>${html}`;
}
