/* What the header knows about the visitor's account, in this page's memory only.

   Nothing here makes a request or stores anything in the browser. The account pages, which already ask the
   server whether this browser is signed in, report the answer here; the header then offers a signed-in
   customer the way around (Chat, My Spaces, Account, Billing, Sign out) instead of Sign in and Create account.
   Until an account page has asked, the state is 'unknown': the homepage, Privacy and Terms never ask, so a
   fresh load of them offers sign-in as before. A full page load starts again from 'unknown'.

   The chat workspace can send a customer here with the Space and conversation they were in
   (/account?space=<id>&conversation=<id>): that pair is kept here, checked, so the account pages and the
   header can offer the way straight back to it. */
import { useSyncExternalStore } from 'react';

export type Presence = 'unknown' | 'signed-in' | 'signed-out';
export type ReturnTarget = { space: string; conversation: string | null };

const ID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const CHAT = '/app/';

let presence: Presence = 'unknown';
let returnTarget: ReturnTarget | null = null;
let signOutHandler: (() => Promise<boolean>) | null = null;
const listeners = new Set<() => void>();
let snapshot: { presence: Presence; returnTarget: ReturnTarget | null } = { presence, returnTarget };

function changed() {
  snapshot = { presence, returnTarget };
  listeners.forEach((listener) => listener());
}

function subscribe(listener: () => void) {
  listeners.add(listener);
  return () => { listeners.delete(listener); };
}

export function setPresence(next: Presence) {
  if (next === presence) return;
  presence = next;
  if (next === 'signed-out') returnTarget = null;
  changed();
}

export function useCustomerSession() {
  return useSyncExternalStore(subscribe, () => snapshot);
}

/* Reads, once, the address the chat opened the account pages with: exactly
   [?show=billing][&]space=<id>[&conversation=<id>], or ?show=billing alone. Any other address is ignored, so
   the way back only ever leads into the chat workspace. Returns whether Billing was asked for. */
const ID_SOURCE = '[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}';
const FROM_CHAT = new RegExp(`^\\?(show=billing&)?space=(${ID_SOURCE})(?:&conversation=(${ID_SOURCE}))?$`);

export function takeArrival(search: string): { billing: boolean } {
  if (search === '?show=billing') return { billing: true };
  const found = FROM_CHAT.exec(search);
  if (!found || !ID.test(found[2]) || (found[3] !== undefined && !ID.test(found[3]))) return { billing: false };
  returnTarget = { space: found[2], conversation: found[3] ?? null };
  changed();
  return { billing: !!found[1] };
}

export function chatHref(target: ReturnTarget | null): string {
  if (!target) return CHAT;
  return `${CHAT}?space=${target.space}${target.conversation ? `&conversation=${target.conversation}` : ''}`;
}

export const SPACES_HREF = `${CHAT}?spaces=open`;

/* The account pages register how to sign out (their own API client); the header calls it. */
export function registerSignOut(handler: () => Promise<boolean>) {
  signOutHandler = handler;
}

export async function signOut(): Promise<boolean> {
  if (!signOutHandler) return false;
  const ok = await signOutHandler();
  if (ok) setPresence('signed-out');
  return ok;
}
