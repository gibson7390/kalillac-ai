/* Account route identity: paths, titles and descriptions. Shared by the client
   (route metadata) and the build (the flat account.html entry). Account pages
   are never indexed, and their canonical URL is the bare path: no fragment,
   query, email or credential ever enters metadata. */
import { PUBLIC_ORIGIN } from '../public-identity';

export const ACCOUNT_PATHS = {
  overview: '/account',
  signIn: '/account/sign-in',
  create: '/account/create',
  recovery: '/account/recovery',
  recoveryConfirm: '/account/recovery/confirm',
  verify: '/account/verify',
} as const;

export const ACCOUNT_ROBOTS = 'noindex, nofollow';

export const ACCOUNT_PAGES: Record<string, { title: string; description: string }> = {
  [ACCOUNT_PATHS.overview]: {
    title: 'Your account — Kalillac AI',
    description: 'Your Kalillac AI account: sign-in email, plan and billing.',
  },
  [ACCOUNT_PATHS.signIn]: {
    title: 'Sign in — Kalillac AI',
    description: 'Sign in to your Kalillac AI account. An account is optional.',
  },
  [ACCOUNT_PATHS.create]: {
    title: 'Create an account — Kalillac AI',
    description: 'Create an optional Kalillac AI account for plan access.',
  },
  [ACCOUNT_PATHS.recovery]: {
    title: 'Reset your password — Kalillac AI',
    description: 'Request a link to reset your Kalillac AI account password.',
  },
  [ACCOUNT_PATHS.recoveryConfirm]: {
    title: 'Choose a new password — Kalillac AI',
    description: 'Choose a new password for your Kalillac AI account.',
  },
  [ACCOUNT_PATHS.verify]: {
    title: 'Verify your email — Kalillac AI',
    description: 'Confirm the email address on your Kalillac AI account.',
  },
};

export function isAccountPath(path: string): boolean {
  return path === ACCOUNT_PATHS.overview || path.startsWith(ACCOUNT_PATHS.overview + '/');
}

export function accountPage(path: string) {
  const page = ACCOUNT_PAGES[path] ?? ACCOUNT_PAGES[ACCOUNT_PATHS.overview];
  const known = path in ACCOUNT_PAGES;
  return { ...page, url: PUBLIC_ORIGIN + (known ? path : ACCOUNT_PATHS.overview) };
}

export function updateAccountMetadata(path: string) {
  const page = accountPage(path);
  document.title = page.title;
  document.querySelector<HTMLLinkElement>('link[rel="canonical"]')?.setAttribute('href', page.url);
  for (const [selector, value] of [
    ['meta[name="robots"]', ACCOUNT_ROBOTS],
    ['meta[property="og:url"]', page.url],
    ['meta[name="description"]', page.description],
    ['meta[property="og:title"]', page.title],
    ['meta[property="og:description"]', page.description],
    ['meta[name="twitter:title"]', page.title],
    ['meta[name="twitter:description"]', page.description],
  ]) document.querySelector(selector)?.setAttribute('content', value);
}
