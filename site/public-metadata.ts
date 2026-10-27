import type { Plugin } from 'vite';
import { PUBLIC_ORIGIN, PUBLIC_PAGES, SOCIAL_IMAGE, publicPage } from './src/public-identity';

const escape = (value: string) => value.replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;');

function pageHtml(html: string, path: string): string {
  const page = publicPage(path);
  if (!page) throw new Error(`Unknown public page: ${path}`);
  html = html.replace(/<title>[^<]*<\/title>/, `<title>${escape(page.title)}</title>`);
  html = html.replace(/(<link rel="canonical" href=")[^"]*(")/, `$1${page.url}$2`);
  for (const [attribute, key, value] of [
    ['property', 'og:url', page.url],
    ['property', 'og:title', page.title],
    ['property', 'og:description', page.description],
    ['property', 'og:image', SOCIAL_IMAGE],
    ['name', 'description', page.description],
    ['name', 'twitter:title', page.title],
    ['name', 'twitter:description', page.description],
    ['name', 'twitter:image', SOCIAL_IMAGE],
  ]) {
    html = html.replace(new RegExp(`(<meta ${attribute}="${key}" content=")[^"]*(")`), `$1${escape(value)}$2`);
  }
  return html;
}

// /app/ remains the separately served, noindex chat; it is not in the sitemap.
export function publicMetadata(): Plugin {
  return {
    name: 'kalillac-public-metadata',
    enforce: 'post',
    transformIndexHtml(html) { return pageHtml(html, '/'); },
    generateBundle: {
      order: 'post',
      handler(_options, bundle) {
        const entry = bundle['index.html'];
        if (!entry || entry.type !== 'asset') throw new Error('Missing site entry HTML');
        const html = String(entry.source);
        for (const path of Object.keys(PUBLIC_PAGES).filter(path => path !== '/')) {
          this.emitFile({ type: 'asset', fileName: `${path.slice(1)}.html`, source: pageHtml(html, path) });
        }
        const urls = Object.keys(PUBLIC_PAGES).map(path => `  <url><loc>${PUBLIC_ORIGIN}${path}</loc></url>`).join('\n');
        this.emitFile({ type: 'asset', fileName: 'sitemap.xml', source:
          `<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n${urls}\n</urlset>\n` });
        this.emitFile({ type: 'asset', fileName: 'robots.txt', source:
          `User-agent: *\nAllow: /\nDisallow: /api/\nDisallow: /app/\nSitemap: ${PUBLIC_ORIGIN}/sitemap.xml\n` });
      },
    },
  };
}
