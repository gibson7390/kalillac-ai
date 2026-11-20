/* The operator's official YouTube and X profiles, in the footer.

   The labels name the platform only: the operator's name and legal form are not stated in public pages
   (test_public_identity.py). No address is written into this code: none has been verified yet, and a guessed one could send visitors to
   someone else's account. A deployment supplies each verified address on the page's <html> element, as it
   supplies its other marks:

       data-kalillac-youtube="https://www.youtube.com/@<verified handle>"
       data-kalillac-x="https://x.com/<verified handle>"

   Each is shown only when it is an https address on that platform's own host; anything else shows nothing. */

import type { ReactElement } from 'react';

type Profile = { key: 'youtube' | 'x'; label: string; hosts: string[]; icon: ReactElement };

const PROFILES: Profile[] = [
  {
    key: 'youtube',
    label: 'YouTube channel',
    hosts: ['www.youtube.com', 'youtube.com'],
    icon: (
      <svg viewBox="0 0 24 24" width="20" height="20" aria-hidden="true" focusable="false">
        <path fill="currentColor" d="M23.5 6.19a3.02 3.02 0 0 0-2.12-2.14C19.5 3.55 12 3.55 12 3.55s-7.5 0-9.38.5A3.02 3.02 0 0 0 .5 6.19C0 8.07 0 12 0 12s0 3.93.5 5.81a3.02 3.02 0 0 0 2.12 2.14c1.87.5 9.38.5 9.38.5s7.5 0 9.38-.5a3.02 3.02 0 0 0 2.12-2.14C24 15.93 24 12 24 12s0-3.93-.5-5.81ZM9.55 15.57V8.43L15.82 12l-6.27 3.57Z" />
      </svg>
    ),
  },
  {
    key: 'x',
    label: 'X profile',
    hosts: ['x.com', 'www.x.com', 'twitter.com', 'www.twitter.com'],
    icon: (
      <svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true" focusable="false">
        <path fill="currentColor" d="M18.9 1.15h3.68l-8.04 9.19L24 22.85h-7.4l-5.8-7.59-6.64 7.59H.47l8.6-9.83L0 1.15h7.6l5.24 6.93 6.06-6.93Zm-1.29 19.5h2.04L6.49 3.24H4.3l13.31 17.41Z" />
      </svg>
    ),
  },
];

export function verifiedProfileUrl(value: string | null, hosts: string[]): string | null {
  if (!value) return null;
  try {
    const url = new URL(value);
    if (url.protocol !== 'https:' || !hosts.includes(url.hostname) || url.username || url.password) return null;
    if (url.pathname.length < 2) return null;                 // A profile, not the platform's home page.
    return url.href;
  } catch {
    return null;
  }
}

export function SocialLinks() {
  const root = document.documentElement;
  const shown = PROFILES
    .map((profile) => ({ ...profile, href: verifiedProfileUrl(root.getAttribute(`data-kalillac-${profile.key}`), profile.hosts) }))
    .filter((profile) => profile.href);
  if (!shown.length) return null;
  return (
    <nav className="footer-social" aria-label="Social media">
      {shown.map((profile) => (
        <a key={profile.key} href={profile.href!} target="_blank" rel="noopener noreferrer"
           aria-label={profile.label} title={profile.label} data-testid={`link-footer-${profile.key}`}>
          {profile.icon}
        </a>
      ))}
    </nav>
  );
}
