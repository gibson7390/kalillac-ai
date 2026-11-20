// DEVELOPMENT PREVIEW ONLY (npx vite --config vite.preview.config.ts): never used by `npm run build` or any deployment.
//
// http://localhost:8020 serves the site from source with hot module replacement, and the chat workspace from
// ../frontend at /app/ with a full reload whenever one of its files changes. Every page carries the marks a
// staging deployment adds (accounts, files, images, reasoning, spaces) and a fixed DEVELOPMENT banner.
// /api and /__local__ go to the preview's own application and panel (candidate/tools/dev_preview.py run, on
// 127.0.0.1:8022), never to local staging (8010/8011) or production.
import fs from 'fs';
import path from 'path';
import type { Plugin, ViteDevServer } from 'vite';
import { defineConfig, mergeConfig } from 'vite';
import base from './vite.config';
import { installPreviewFence } from './preview-fence.mjs';

// This Vite process's own network fence: no connection to local staging's ports or off this machine.
installPreviewFence();

const FRONTEND = path.resolve(import.meta.dirname, '../frontend');
const PANEL = 'http://127.0.0.1:8022';
const SITE_MARKS = ['accounts', 'files'];
const APP_MARKS = ['accounts', 'files', 'images', 'reasoning', 'spaces'];
const HTML = '<html lang="en">';
const TYPES: Record<string, string> = {
  '.js': 'text/javascript', '.css': 'text/css', '.html': 'text/html', '.svg': 'image/svg+xml',
  '.png': 'image/png', '.woff2': 'font/woff2', '.woff': 'font/woff', '.ttf': 'font/ttf', '.json': 'application/json',
};

const BANNER = `<div id="kalillac-development-banner" role="status" style="position:fixed;z-index:2147483647;left:0;right:0;bottom:0;padding:4px 10px;background:#7c2d12;color:#fff;font:600 12px/1.4 system-ui,sans-serif;text-align:center;letter-spacing:.04em;pointer-events:none">DEVELOPMENT PREVIEW &middot; synthetic data &middot; simulated providers and payments &middot; not Kalillac production</div>`;

// The footer's YouTube and X profile addresses, only when the owner supplies verified ones:
//   KALILLAC_PREVIEW_YOUTUBE=https://www.youtube.com/@... KALILLAC_PREVIEW_X=https://x.com/... npx vite --config vite.preview.config.ts
// None is guessed; without them the footer shows no profile links (src/social-links.tsx checks the hosts).
const PROFILES = ([['youtube', process.env.KALILLAC_PREVIEW_YOUTUBE], ['x', process.env.KALILLAC_PREVIEW_X]] as const)
  .filter(([, url]) => url && /^https:\/\/[^\s"<>&]+$/.test(url))
  .map(([key, url]) => ` data-kalillac-${key}="${url}"`).join('');

function marked(html: string, marks: string[]): string {
  if (html.split(HTML).length !== 2) throw new Error('a page\'s <html> element is not the expected one');
  return html.replace(HTML, `<html lang="en"${marks.map((m) => ` data-kalillac-${m}="on"`).join('')}${PROFILES} data-kalillac-development="on">`)
    .replace('</body>', `${BANNER}</body>`);
}

function kalillacPreview(): Plugin {
  return {
    name: 'kalillac-development-preview',
    apply: 'serve',
    transformIndexHtml: { order: 'post', handler: (html: string) => marked(html, SITE_MARKS) },
    configureServer(server: ViteDevServer) {
      server.watcher.add(FRONTEND);
      server.watcher.on('change', (file) => {
        if (path.resolve(file).startsWith(FRONTEND + path.sep)) server.ws.send({ type: 'full-reload' });
      });
      // /app/ (and /app) from ../frontend, as production and staging serve it.
      server.middlewares.use((req, res, next) => {
        const url = new URL(req.url ?? '/', 'http://localhost');
        if (url.pathname === '/app') {
          res.statusCode = 301;
          res.setHeader('Location', '/app/');
          res.end();
          return;
        }
        if (!url.pathname.startsWith('/app/')) return next();
        const relative = decodeURIComponent(url.pathname.slice('/app/'.length)) || 'index.html';
        const file = path.resolve(FRONTEND, relative);
        if (!file.startsWith(FRONTEND + path.sep) || !fs.existsSync(file) || !fs.statSync(file).isFile()) {
          return next();
        }
        res.setHeader('Cache-Control', 'no-store');
        if (relative === 'index.html') {
          // The Vite client connects back for the full reload when a chat file changes.
          const page = marked(fs.readFileSync(file, 'utf-8'), APP_MARKS)
            .replace('</head>', '<script type="module" src="/@vite/client"></script></head>');
          res.setHeader('Content-Type', 'text/html; charset=utf-8');
          res.end(page);
          return;
        }
        res.setHeader('Content-Type', TYPES[path.extname(file)] ?? 'application/octet-stream');
        fs.createReadStream(file).pipe(res);
      });
    },
  };
}

export default mergeConfig(base, defineConfig({
  plugins: [kalillacPreview()],
  server: {
    host: '127.0.0.1',
    port: 8020,
    strictPort: true,
    fs: { allow: [path.resolve(import.meta.dirname), FRONTEND] },
    proxy: {
      '/api': { target: PANEL, changeOrigin: false },
      '/__local__': { target: PANEL, changeOrigin: false },
    },
  },
}));
