// Public identity shared by the build and client-side route metadata.
export const PUBLIC_ORIGIN = 'https://kalillac.ai';
export const SOCIAL_IMAGE = `${PUBLIC_ORIGIN}/assets/kalillac-ai-logo.png`;
export const PUBLIC_PAGES: Record<string, { title: string; description: string }> = {
  '/': {
    title: 'Kalillac AI — Private by design',
    description: 'Kalillac AI is an assistant for questions, writing, code, and current web information, used in temporary sessions without an account.',
  },
  '/privacy': {
    title: 'Privacy — Kalillac AI',
    description: 'How Kalillac AI handles temporary conversations, daily allowance records, cookies, providers, and infrastructure.',
  },
  '/terms': {
    title: 'Terms of Use — Kalillac AI',
    description: 'Terms for using Kalillac AI, a free AI assistant for adults 18 and older, with a daily response allowance.',
  },
};

export function publicPage(path: string) {
  const page = PUBLIC_PAGES[path];
  return page ? { ...page, url: PUBLIC_ORIGIN + path } : null;
}

// Keep navigation relative while canonical identity always uses the apex origin.
export function updatePublicMetadata(path: string) {
  const page = publicPage(path);
  if (!page) return;
  document.title = page.title;
  document.querySelector<HTMLLinkElement>('link[rel="canonical"]')?.setAttribute('href', page.url);
  for (const [selector, value] of [
    ['meta[property="og:url"]', page.url],
    ['meta[name="description"]', page.description],
    ['meta[property="og:title"]', page.title],
    ['meta[property="og:description"]', page.description],
    ['meta[name="twitter:title"]', page.title],
    ['meta[name="twitter:description"]', page.description],
  ]) document.querySelector(selector)?.setAttribute('content', value);
}
