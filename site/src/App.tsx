import { type ReactNode, type RefObject, Suspense, lazy, useCallback, useEffect, useRef, useState } from 'react';
import { flushSync } from 'react-dom';
import { ArrowRight, ArrowUp, CircleSlash, Clock, Code2, FileText, Lightbulb, Menu, MessageSquare, PenLine, Search, ShieldCheck, Smartphone, Sparkles, UserRound, X, type LucideIcon } from 'lucide-react';
import { type AroundNavHandler, Link, Route, Switch, Router as WouterRouter, useLocation } from 'wouter';
import { ErrorBoundary } from '@/components/error-boundary';
import NotFound from '@/pages/not-found';
import { updatePublicMetadata } from '@/public-identity';
import { isAccountPath, updateAccountMetadata } from '@/account/account-routes';

// Account pages load only on /account routes: the homepage, Privacy and Terms
// never request their code or styles.
const AccountPages = lazy(() => import('@/account/account-pages'));

const brandLogoUrl = '/assets/kalillac-ai-logo.png';

// The canonical chat workspace (../frontend), served at /app/. The homepage
// never loads or frames it: only Send opens it, with the draft.
const CHAT_PATH = '/app/';

// A real load of /app/ is the canonical chat in ../frontend; this site must
// never frame itself if it is ever served there.
const IS_FRAMED = (() => {
  try { return window.self !== window.top; } catch { return true; }
})();


function SiteHeader() {
  const [open, setOpen] = useState(false);
  const toggle = useRef<HTMLButtonElement>(null);
  // Styling hook only: the header marks the current page's own link.
  const [location] = useLocation();
  const close = () => setOpen(false);
  useEffect(() => {
    if (!open) return;
    const escape = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return;
      event.preventDefault();
      close();
      toggle.current?.focus({ preventScroll: true });
    };
    document.addEventListener('keydown', escape);
    return () => document.removeEventListener('keydown', escape);
  }, [open]);
  return (
    <header className="site-header" data-route={location.slice(1) || 'home'}>
      <div className="container-wide site-header-inner">
        <Link href="/" className="brand" data-testid="link-home-brand" onClick={close} aria-label="Kalillac AI home">
          <img className="brand-logo" src={brandLogoUrl} alt="Kalillac AI" />
        </Link>
        <nav className="site-nav" aria-label="Site navigation">
          <a href="/#product" className="nav-link" data-testid="link-product-nav">Product</a>
          <Link href="/privacy" className="nav-link" data-testid="link-privacy-nav">Privacy</Link>
          <a href="/#how-it-works" className="nav-link" data-testid="link-how-nav">How it works</a>
          <button ref={toggle} type="button" className="mobile-nav-toggle" onClick={() => setOpen(!open)} aria-expanded={open} aria-controls="mobile-site-menu" aria-label={open ? 'Close navigation menu' : 'Open navigation menu'} data-testid="button-mobile-menu">
            {open ? <X size={20} aria-hidden="true" /> : <Menu size={20} aria-hidden="true" />}
          </button>
        </nav>
      </div>
      {open && (
        <nav id="mobile-site-menu" className="mobile-site-menu" aria-label="Mobile navigation">
          <a href="/#product" onClick={close} data-testid="link-mobile-product">Product</a>
          <Link href="/privacy" onClick={close} data-testid="link-mobile-privacy">Privacy</Link>
          <a href="/#how-it-works" onClick={close} data-testid="link-mobile-how">How it works</a>
        </nav>
      )}
    </header>
  );
}

function SiteFooter() {
  return (
    <footer className="site-footer">
      <div className="container-wide footer-inner">
        <div className="footer-brand"><Link href="/" className="brand" data-testid="link-footer-home"><img className="brand-logo" src={brandLogoUrl} alt="Kalillac AI" /></Link><p>A more private, helpful internet.</p></div>
        <nav className="footer-links" aria-label="Footer navigation">
          <a href="/#product" data-testid="link-footer-product">Product</a>
          <Link href="/privacy" data-testid="link-footer-privacy">Privacy</Link>
          <a href="/#how-it-works" data-testid="link-footer-how">How it works</a>
          <Link href="/terms" data-testid="link-footer-terms">Terms</Link>
          <a href="https://www.linkedin.com/in/kalillacai" target="_blank" rel="noopener noreferrer" data-testid="link-footer-linkedin">LinkedIn</a>
        </nav>
      </div>
      <div className="container-wide footer-bottom"><span>&copy; 2026 Kalillac AI &middot; Adults 18+</span><span>Kalillac can make mistakes. Verify important information.</span></div>
    </footer>
  );
}

function OrbitalAssistant() {
  return <div className="orbital-wrap" aria-hidden="true"><span className="orbit-dot blue"/><span className="orbit-dot mint"/></div>;
}

function prefersReducedMotion(): boolean {
  return typeof window.matchMedia === 'function' &&
    window.matchMedia('(prefers-reduced-motion: reduce)').matches;
}

/* The homepage and the chat at /app/ are separate documents, and only Send
   moves a draft between them: one versioned envelope in this tab's
   sessionStorage holding the trimmed draft, a random single-use nonce and its
   creation time. ../frontend/app.js removes it before sending the message,
   and only a valid, fresh, unused envelope is ever sent. This page never
   contacts the chat service, and stores nothing else -- except, when it
   cannot remove an envelope it takes back, a withdrawn marker in its place
   that holds the nonce and this document's random identity, and no message.
   It only reads app.js's ledger of the
   nonces the chat has taken. The keys, shapes and limits must match app.js. */
const HANDOFF_KEY = 'kalillac.handoff';
const HANDOFF_LEDGER_KEY = 'kalillac.handoff.ledger';
const HANDOFF_LEDGER_MAX = 32;
const HANDOFF_VERSION = 1;
// After this the chat never takes an envelope (app.js's HANDOFF_MAX_AGE_MS).
const HANDOFF_MAX_AGE_MS = 60_000;
/* How long app.js keeps a taken nonce in its ledger: an entry expires at the
   envelope's createdAt plus this (app.js's HANDOFF_RETAIN_MS). */
const HANDOFF_RETAIN_MS = HANDOFF_MAX_AGE_MS + 5_000;
/* After Send, how long this page waits for the chat to open before giving the
   draft back. app.js never sends an envelope 8 seconds old or older
   (HANDOFF_SEND_MS), so by then the chat cannot send it any more. */
const HANDOFF_STALL_MS = 10_000;
// The chat service's own limit (MAX_INPUT_CHARS), in code points of the trimmed message.
const MESSAGE_MAX_CHARS = 4000;
const NONCE = /^[0-9a-f]{32}$/;

type HandoffEnvelope = { v: number; draft: string; nonce: string; createdAt: number };
/* A handoff this page answers for: the exact draft, its envelope's nonce and
   time -- and whether a withdrawal this page could not confirm may have
   removed the envelope, so that its absence no longer shows the chat took it. */
type Handoff = Pick<HandoffEnvelope, 'draft' | 'nonce' | 'createdAt'> & { mayBeGone?: boolean };

function codePoints(text: string): number { return Array.from(text).length; }

function newNonce(): string {
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  return Array.from(bytes, byte => byte.toString(16).padStart(2, '0')).join('');
}

/* This document's own random identity, in page memory only: a new one for
   every load or reload of the homepage, the same one when the browser restores
   this very page from its back/forward cache. A withdrawn marker carries the
   identity of the document that wrote it, so only that document can give its
   draft back -- never another homepage holding the same handoff. */
const DOCUMENT_ID = newNonce();

/* What this tab's envelope key holds, read without changing it: nothing, an
   unreadable store, a well-formed envelope (of any age), the marker of an
   envelope a homepage document withdrew (and which one), or something else. */
type Seen = { kind: 'absent' | 'unreadable' | 'other' } | { kind: 'withdrawn'; nonce: string; by: string }
  | { kind: 'envelope'; envelope: HandoffEnvelope };

function readEnvelope(): Seen {
  let raw: string | null;
  try { raw = window.sessionStorage.getItem(HANDOFF_KEY); } catch { return { kind: 'unreadable' }; }
  if (raw === null) return { kind: 'absent' };
  try {
    const value: unknown = JSON.parse(raw);
    if (!value || typeof value !== 'object' || Array.isArray(value)) return { kind: 'other' };
    const marker = value as { v?: unknown; withdrawn?: unknown; by?: unknown };
    if (Object.keys(marker).sort().join() === 'by,v,withdrawn' && marker.v === HANDOFF_VERSION
        && typeof marker.withdrawn === 'string' && NONCE.test(marker.withdrawn)
        && typeof marker.by === 'string' && NONCE.test(marker.by)) {
      return { kind: 'withdrawn', nonce: marker.withdrawn, by: marker.by };
    }
    const envelope = value as HandoffEnvelope;
    if (Object.keys(envelope).sort().join() !== 'createdAt,draft,nonce,v' || envelope.v !== HANDOFF_VERSION
        || typeof envelope.draft !== 'string' || typeof envelope.nonce !== 'string' || !NONCE.test(envelope.nonce)
        || !Number.isSafeInteger(envelope.createdAt) || !envelope.draft.trim()
        || codePoints(envelope.draft) > MESSAGE_MAX_CHARS) return { kind: 'other' };
    return { kind: 'envelope', envelope };
  } catch {
    return { kind: 'other' };
  }
}

/* Withdraws the envelope with this nonce: it is removed or, when removal
   fails or has no effect, overwritten with a withdrawn marker naming this
   document -- no message, which app.js discards like any invalid value -- and
   either is confirmed by reading storage back. 'kept' when neither took
   effect; 'unchecked' when the confirming read failed. */
function withdrawEnvelope(nonce: string): 'withdrawn' | 'kept' | 'unchecked' {
  let removed = false;
  try { window.sessionStorage.removeItem(HANDOFF_KEY); removed = true; } catch { /* overwrite it instead */ }
  if (removed) {
    try { if (window.sessionStorage.getItem(HANDOFF_KEY) === null) return 'withdrawn'; } catch { return 'unchecked'; }
  }
  const marker = JSON.stringify({ v: HANDOFF_VERSION, withdrawn: nonce, by: DOCUMENT_ID });
  try { window.sessionStorage.setItem(HANDOFF_KEY, marker); } catch { return 'kept'; }
  try { return window.sessionStorage.getItem(HANDOFF_KEY) === marker ? 'withdrawn' : 'kept'; } catch { return 'unchecked'; }
}

// Removes what holds no message (a withdrawn marker, or a value that is no valid envelope); failure is harmless.
function discardEnvelope() {
  try { window.sessionStorage.removeItem(HANDOFF_KEY); } catch { /* the chat discards it too */ }
}

/* The nonces the chat has taken, each with its expiry -- app.js's ledger,
   read without changing it and validated exactly as app.js validates it -- or
   null when it cannot be read or is not exactly that shape: then who has a
   handoff cannot be told. */
function takenNonces(): Map<string, number> | null {
  let raw: string | null;
  try { raw = window.sessionStorage.getItem(HANDOFF_LEDGER_KEY); } catch { return null; }
  if (raw === null) return new Map();
  try {
    const ledger: unknown = JSON.parse(raw);
    if (!ledger || typeof ledger !== 'object' || Array.isArray(ledger)) return null;
    const { v, entries } = ledger as { v?: unknown; entries?: unknown };
    if (Object.keys(ledger).sort().join() !== 'entries,v' || v !== 1 || !Array.isArray(entries)
        || entries.length > HANDOFF_LEDGER_MAX) return null;
    const nonces = new Map<string, number>();
    for (const entry of entries as unknown[]) {
      if (!entry || typeof entry !== 'object' || Array.isArray(entry)) return null;
      const { n, x } = entry as { n?: unknown; x?: unknown };
      if (Object.keys(entry).sort().join() !== 'n,x' || typeof n !== 'string' || !NONCE.test(n)
          || !Number.isSafeInteger(x) || (x as number) <= 0 || nonces.has(n)) return null;
      nonces.set(n, x as number);
    }
    return nonces;
  } catch {
    return null;
  }
}

/* Whether a nonce missing from the ledger may have been listed and pruned
   since. app.js prunes an entry only when it records a later handoff at or
   after the entry's expiry (createdAt + HANDOFF_RETAIN_MS), and that later
   entry expires after it. So before that expiry nothing can have been pruned,
   and after it only a ledger holding an entry that expires later may have
   lost this one. */
function mayHaveBeenPruned(taken: Map<string, number>, handoff: Handoff): boolean {
  const expiry = handoff.createdAt + HANDOFF_RETAIN_MS;
  if (Date.now() < expiry) return false;
  for (const x of taken.values()) if (x > expiry) return true;
  return false;
}

/* Who has this handoff now. The chat's ledger decides first, and for good: a
   nonce in it is the chat's even while its envelope is still here (app.js
   records the nonce before it shows or sends the draft, even when it could
   not remove the envelope). The chat records every envelope it takes unless
   it removed it, so an envelope of this nonce still here, or this page's
   withdrawn marker, is the page's -- and an envelope gone within the time the
   chat takes envelopes, when this page did not remove it, is the chat's. A
   nonce missing from the ledger shows the chat never took it only while it
   cannot have been pruned ('too-old' otherwise; a withdrawn marker still
   shows this page withdrew it). Anything else -- an unreadable or malformed
   ledger or store, a replaced envelope, a removal this page may have made
   itself, an envelope gone after the chat's acceptance window -- cannot be
   told. A withdrawn marker is this page's only when it names this document;
   one written by another homepage document ('elsewhere') shows that document
   took the draft back, so this page must not offer it too. */
type Owner = 'chat' | 'untaken' | 'withdrawn' | 'elsewhere' | 'unknown' | 'too-old';

function ownerOf(handoff: Handoff): Owner {
  const taken = takenNonces();
  if (taken === null) return 'unknown';
  if (taken.has(handoff.nonce)) return 'chat';
  const seen = readEnvelope();
  if (seen.kind === 'withdrawn' && seen.nonce === handoff.nonce) {
    return seen.by === DOCUMENT_ID ? 'withdrawn' : 'elsewhere';
  }
  if (mayHaveBeenPruned(taken, handoff)) return 'too-old';
  if (seen.kind === 'envelope' && seen.envelope.nonce === handoff.nonce) return 'untaken';
  if (seen.kind === 'absent' && !handoff.mayBeGone && Date.now() - handoff.createdAt < HANDOFF_MAX_AGE_MS) {
    return 'chat';
  }
  return 'unknown';
}

const DEFAULT_PLACEHOLDER = 'Message Kalillac AI…';
const COPY_TOO_LONG = 'That message is too long. Please shorten it and try again.';
const COPY_STORAGE_FAILED = 'Your message wasn’t sent: this browser didn’t let the page pass it to the chat. '
  + 'It is still here. Try again, or open the chat and paste it there.';
const COPY_RETURNED = 'Your message wasn’t sent. It is back in the message box.';
const COPY_STALLED = 'The chat didn’t open, so your message wasn’t sent. It is back in the message box.';
/* Read-only notices, for a draft this page keeps but cannot give back: until
   a later showing can withdraw it, or can tell who has it. */
const COPY_HELD = 'Your message hasn’t been sent, but this browser didn’t let the page withdraw it from the chat. '
  + 'So that it can’t be sent twice, it stays here read-only. You can still select and copy it.';
const COPY_STALLED_HELD = 'The chat didn’t open, so your message wasn’t sent, but this browser didn’t let the page '
  + 'withdraw it. So that it can’t be sent twice, it stays here read-only. You can still select and copy it.';
const COPY_HELD_UNKNOWN = 'This browser didn’t let the page check whether the chat took your message. So that it '
  + 'can’t be sent twice, it stays here read-only. You can still select and copy it.';
const COPY_HELD_TOO_OLD = 'This page can no longer check whether the chat took your message, because the chat has '
  + 'taken a newer one since. So that it can’t be sent twice, it stays here read-only. You can still select and '
  + 'copy it.';
const COPY_HELD_ELSEWHERE = 'Another Kalillac page in this tab took this message back. So that it can’t be sent '
  + 'twice, it stays here read-only. You can still select and copy it.';
type HoldKind = 'unsent' | 'stalled' | 'unknown' | 'too-old' | 'elsewhere';
const COPY_HOLD: Record<HoldKind, string> = {
  unsent: COPY_HELD, stalled: COPY_STALLED_HELD, unknown: COPY_HELD_UNKNOWN, 'too-old': COPY_HELD_TOO_OLD,
  elsewhere: COPY_HELD_ELSEWHERE,
};
// The read-only hold for an owner this page cannot give the draft back to.
function holdFor(owner: Owner): HoldKind {
  return owner === 'too-old' || owner === 'elsewhere' ? owner : 'unknown';
}

/* The chat's own empty-state examples and icons (../frontend/index.html). */
const starters = [
  { kind: 'research', title: 'Research the current web', description: 'Get up-to-date information with sources.',
    example: 'Find this week’s biggest technology stories', icon: <><circle cx="10.5" cy="10.5" r="7.5"/><path d="m16 16 5 5"/></> },
  { kind: 'writing', title: 'Write and revise', description: 'Improve your writing, brainstorm, and get feedback.',
    example: 'Draft a professional email',
    icon: <><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8Z"/><path d="M14 2v6h6M8 13h8M8 17h6"/></> },
  { kind: 'code', title: 'Code and debug', description: 'Build, fix, and explain code in plain language.',
    example: 'Build a simple Python password generator', icon: <path d="m8 5-7 7 7 7m8-14 7 7-7 7"/> },
  { kind: 'learning', title: 'Learn anything', description: 'Get clear explanations on complex topics.',
    example: 'Explain how encryption works', icon: <path d="M9 18h6M9 22h6M9 14c-1-1-3-3-3-6a6 6 0 0 1 12 0c0 3-2 5-3 6v1H9Z"/> },
];

function ChatIcon({ className, children }: { className?: string; children: ReactNode }) {
  return <svg className={className} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round"
    strokeLinejoin="round" aria-hidden="true" focusable="false">{children}</svg>;
}

type ComposerNotice = { kind: 'status' | 'alert'; text: string } | null;

/* The homepage composer: a real message box on this page. Typing, editing,
   clearing and the examples never navigate, store or send anything; Send
   alone hands the trimmed draft to /app/, once. The sent draft leaves the box
   before the page can be cached, so Back never shows it again as unsent, and
   a draft the chat never took comes back with a notice. */
function HomeComposer({ slotRef, inputRef }: { slotRef: RefObject<HTMLDivElement | null>;
  inputRef: RefObject<HTMLTextAreaElement | null> }) {
  const [draft, setDraft] = useState('');
  const [placeholder, setPlaceholder] = useState(DEFAULT_PLACEHOLDER);
  const [notice, setNotice] = useState<ComposerNotice>(null);
  const [handingOff, setHandingOff] = useState(false);
  const draftRef = useRef(draft);
  draftRef.current = draft;
  const handingOffRef = useRef(false);
  /* The handoff this page answers for, from Send until the chat has taken it
     or this page has confirmed its withdrawal: a chat that never opens, and a
     Back before the chat took it, give back exactly this draft. Leaving the
     page keeps it; only timers stop. */
  const pending = useRef<Handoff | null>(null);
  const stallTimer = useRef<number | null>(null);
  const composing = useRef(false);

  const message = draft.trim();
  const length = codePoints(message);
  const tooLong = length > MESSAGE_MAX_CHARS;
  const canSend = message.length > 0 && !tooLong && !handingOff;

  const stopTimers = useCallback(() => {
    if (stallTimer.current !== null) window.clearTimeout(stallTimer.current);
    stallTimer.current = null;
  }, []);

  const unlock = useCallback(() => {
    stopTimers();
    handingOffRef.current = false;
    setHandingOff(false);
  }, [stopTimers]);

  // The box is the draft's only home again, with this notice.
  const giveBack = useCallback((text: string, next: ComposerNotice) => {
    pending.current = null;
    unlock();
    setDraft(text);
    setNotice(next);
  }, [unlock]);

  /* A handoff this page cannot give back stays here, read-only: one the chat
     has not taken but this page could not confirm it withdrew ('unsent', or
     'stalled' after the chat failed to open), one whose owner cannot be told
     ('unknown'), or one too old to check ('too-old'). None is released by
     time: even once the chat can no longer take it, an envelope this page
     could not withdraw may still be here for another page to give back. Only
     a later showing of this page that withdraws it, or can tell who has it,
     decides. */
  const hold = useCallback((handoff: Handoff, kind: HoldKind) => {
    stopTimers();
    pending.current = handoff;
    handingOffRef.current = true;
    setHandingOff(true);
    setDraft(handoff.draft);
    setNotice({ kind: 'alert', text: COPY_HOLD[kind] });
  }, [stopTimers]);

  /* Withdraws a handoff the chat has not taken: a confirmed withdrawal gives
     the draft back as unsent; otherwise the draft is held. */
  const takeBack = useCallback((handoff: Handoff, stalled: boolean) => {
    const withdrawal = withdrawEnvelope(handoff.nonce);
    if (withdrawal === 'withdrawn') {
      giveBack(handoff.draft, { kind: 'status', text: stalled ? COPY_STALLED : COPY_RETURNED });
    } else {
      hold({ ...handoff, mayBeGone: handoff.mayBeGone || withdrawal === 'unchecked' }, stalled ? 'stalled' : 'unsent');
    }
  }, [giveBack, hold]);

  /* On every showing of this page -- a load, a reload, or a Back that
     restores it from the cache -- decide who has the draft. */
  const reclaim = useCallback(() => {
    stopTimers();
    const mine = pending.current;
    if (mine) {
      const owner = ownerOf(mine);
      if (owner === 'chat') {
        // The chat took ownership: the page is clean, and the chat's envelope is left alone.
        giveBack('', null);
      } else if (owner === 'withdrawn') {
        discardEnvelope();
        giveBack(mine.draft, { kind: 'status', text: COPY_RETURNED });
      } else if (owner === 'untaken') {
        takeBack(mine, false);
      } else {
        // Nothing is removed unread, and the draft is not offered as a second copy.
        hold(mine, holdFor(owner));
      }
      return;
    }
    /* Not this page's own handoff: a valid envelope, of any age, is never
       discarded. One the chat took is left to it; one it has not taken is
       withdrawn and given back -- or, until that can be confirmed, or while who
       has it cannot be told, kept here read-only. */
    const seen = readEnvelope();
    if (seen.kind === 'envelope' && draftRef.current === '') {
      const { draft: found, nonce, createdAt } = seen.envelope;
      const handoff: Handoff = { draft: found, nonce, createdAt };
      const owner = ownerOf(handoff);
      if (owner === 'chat') {
        unlock();
        setNotice(null);
      } else if (owner === 'untaken') {
        takeBack(handoff, false);
      } else {
        hold(handoff, holdFor(owner));
      }
      return;
    }
    // Only what holds no message is removed: a withdrawn marker, or a value that is no valid envelope.
    if (seen.kind === 'withdrawn' || seen.kind === 'other') discardEnvelope();
    unlock();
    setNotice(null);
  }, [giveBack, hold, stopTimers, takeBack, unlock]);

  /* The chat did not open in time. This page has been showing since Send,
     so no chat has taken the draft: it comes back from page memory and the
     envelope is withdrawn, whether or not storage can still be read -- or,
     when the withdrawal cannot be confirmed, the draft stays here read-only.
     The navigation still on its way is
     stopped, so this page and the draft stay even where the browser would
     not keep the page for Back. */
  const recover = useCallback(() => {
    const mine = pending.current;
    if (!mine) return;
    window.stop();
    takeBack(mine, true);
  }, [takeBack]);

  useEffect(() => {
    const onShow = () => reclaim();
    // Leaving the page stops the timers; the pending handoff stays until the next showing decides.
    const onHide = () => stopTimers();
    window.addEventListener('pageshow', onShow);
    window.addEventListener('pagehide', onHide);
    reclaim();
    return () => {
      window.removeEventListener('pageshow', onShow);
      window.removeEventListener('pagehide', onHide);
      stopTimers();
    };
  }, [reclaim, stopTimers]);

  // The box grows with its text up to its CSS maximum, then scrolls.
  useEffect(() => {
    const input = inputRef.current;
    if (!input) return;
    input.style.height = 'auto';
    input.style.height = `${Math.min(input.scrollHeight, 220)}px`;
  }, [draft, inputRef]);

  const send = () => {
    if (handingOffRef.current) return;
    const text = draftRef.current.trim();
    if (!text) return;
    if (codePoints(text) > MESSAGE_MAX_CHARS) { setNotice({ kind: 'alert', text: COPY_TOO_LONG }); return; }
    handingOffRef.current = true;
    const envelope: HandoffEnvelope = { v: HANDOFF_VERSION, draft: text, nonce: newNonce(), createdAt: Date.now() };
    const serialized = JSON.stringify(envelope);
    let stored = false;
    try {
      window.sessionStorage.setItem(HANDOFF_KEY, serialized);
      stored = window.sessionStorage.getItem(HANDOFF_KEY) === serialized;
    } catch {
      stored = false;
    }
    if (!stored) {
      try { window.sessionStorage.removeItem(HANDOFF_KEY); } catch { /* nothing was stored */ }
      handingOffRef.current = false;
      setNotice({ kind: 'alert', text: COPY_STORAGE_FAILED });
      return;
    }
    pending.current = { draft: text, nonce: envelope.nonce, createdAt: envelope.createdAt };
    // Synchronously, so the page the browser may cache already has an empty box.
    flushSync(() => {
      setDraft('');
      setPlaceholder(DEFAULT_PLACEHOLDER);
      setHandingOff(true);
      setNotice({ kind: 'status', text: 'Opening the chat…' });
    });
    stallTimer.current = window.setTimeout(recover, HANDOFF_STALL_MS);
    window.location.assign(CHAT_PATH);
  };

  const clearDraft = () => {
    setDraft('');
    setPlaceholder(DEFAULT_PLACEHOLDER);
    setNotice({ kind: 'status', text: 'Draft cleared.' });
    inputRef.current?.focus({ preventScroll: true });
  };

  /* An example focuses the box. An empty box shows it as a suggestion; a
     draft is never replaced. */
  const suggest = (example: string) => {
    if (draftRef.current === '') setPlaceholder(`Try: ${example}`);
    inputRef.current?.focus({ preventScroll: true });
  };

  return (
    <div id="chat" className="home-composer-slot" ref={slotRef}>
      <section className="home-composer" aria-labelledby="home-composer-title" data-testid="home-composer">
        <div className="hc-head">
          <h2 id="home-composer-title" className="hc-title">What are you working on?</h2>
          <p className="hc-line">Bring a difficult question, a rough draft, broken code, or something current.</p>
        </div>
        <form className="hc-form" autoComplete="off" noValidate onSubmit={event => { event.preventDefault(); send(); }}>
          <div className="hc-shell" data-busy={handingOff || undefined}
            onMouseDown={event => {
              // The box's own padding is part of the typing area.
              if (event.button !== 0 || (event.target as HTMLElement).closest('button, textarea, a')) return;
              event.preventDefault();
              inputRef.current?.focus({ preventScroll: true });
            }}>
            <label htmlFor="home-composer-input" className="sr-only">Message Kalillac AI</label>
            <textarea id="home-composer-input" ref={inputRef} className="hc-input" rows={2} value={draft}
              placeholder={placeholder} autoComplete="off" spellCheck enterKeyHint="send" readOnly={handingOff}
              aria-describedby="home-composer-hint" aria-invalid={tooLong || undefined} data-testid="input-home-composer"
              onChange={event => { setDraft(event.target.value); setNotice(null); }}
              onCompositionStart={() => { composing.current = true; }}
              onCompositionEnd={() => { composing.current = false; }}
              onKeyDown={event => {
                if (event.key !== 'Enter' || event.shiftKey) return;
                // An IME is still composing: Enter confirms its text, never sends.
                if (event.nativeEvent.isComposing || composing.current || event.keyCode === 229) return;
                // On touch keyboards Enter is a new line; the Send button sends.
                if (typeof window.matchMedia === 'function' && window.matchMedia('(pointer: coarse)').matches) return;
                event.preventDefault();
                send();
              }} />
            <div className="hc-actions">
              <button type="button" className="hc-clear" disabled={draft === '' || handingOff} onClick={clearDraft}
                data-testid="button-home-clear">
                <ChatIcon><path d="M18 6 6 18M6 6l12 12"/></ChatIcon><span>Clear draft</span>
              </button>
              {length > MESSAGE_MAX_CHARS - 400 &&
                <span className={`hc-count${tooLong ? ' is-over' : ''}`} data-testid="home-composer-count">
                  {length.toLocaleString('en-US')} / {MESSAGE_MAX_CHARS.toLocaleString('en-US')}</span>}
              <button type="submit" className="hc-send" aria-label="Send message" title="Send" disabled={!canSend}
                data-testid="button-home-send"><ArrowUp size={19} strokeWidth={2.2} aria-hidden="true"/></button>
            </div>
          </div>
          <p id="home-composer-hint" className="hc-hint">Nothing is sent until you press Send. Sending opens the chat.</p>
          <p className="hc-notice" role="status" aria-live="polite" data-testid="home-composer-status">
            {notice?.kind === 'status' ? notice.text : tooLong ? COPY_TOO_LONG : ''}</p>
          <p className="hc-notice hc-notice-alert" role="alert" data-testid="home-composer-alert">
            {notice?.kind === 'alert' ? notice.text : ''}</p>
        </form>
        <div className="hc-starter">
          <p className="hc-starter-label" id="home-examples-label">Or start with an example</p>
          <div className="hc-cards" role="group" aria-labelledby="home-examples-label">
            {starters.map(({ kind, title, description, example, icon }) => (
              <button key={kind} type="button" className={`hc-card hc-card-${kind}`} data-testid="home-card" data-kind={kind}
                aria-label={title} aria-describedby={`hc-card-${kind}-description`} onClick={() => suggest(example)}>
                <span className="hc-card-icon"><ChatIcon>{icon}</ChatIcon></span>
                <span className="hc-card-copy"><span className="hc-card-title">{title}</span>
                  <span className="hc-card-description" id={`hc-card-${kind}-description`}>{description}</span></span>
              </button>))}
          </div>
        </div>
      </section>
    </div>
  );
}

/* Bring the composer to just below the sticky header when it is not already
   fully visible there -- smoothly, or at once under reduced motion -- then
   focus it without a second, browser-generated jump. Never navigates. */
function focusComposer(slot: HTMLElement | null, input: HTMLTextAreaElement | null) {
  if (!slot || !input) return;
  const header = document.querySelector('.site-header');
  const headerBottom = header ? header.getBoundingClientRect().bottom : 0;
  const offset = parseFloat(getComputedStyle(slot).scrollMarginTop) || headerBottom;
  const box = input.closest('.hc-shell') ?? input;
  const rect = box.getBoundingClientRect();
  const fullyVisible = rect.top >= headerBottom && rect.bottom <= window.innerHeight;
  if (!fullyVisible) {
    window.scrollTo({
      top: Math.max(0, window.scrollY + slot.getBoundingClientRect().top - offset),
      // 'auto' (not the newer 'instant', which older browsers reject) is immediate:
      // under reduced motion the site CSS forces scroll-behavior: auto.
      behavior: prefersReducedMotion() ? 'auto' : 'smooth',
    });
  }
  input.focus({ preventScroll: true });
}

/* Meaningful flow content is HTML; arrows are decorative. */
function FlowMarkers() { return <ArrowRight className="flow-arrow" size={22} aria-hidden="true"/>; }

const tools: { title: string; description: string; kind: string; icon: LucideIcon }[] = [
  { title: 'Research the current web', description: 'Find current information with sources. Brave is primary; Tavily may provide a bounded fallback or follow-up when eligible.', kind: 'research', icon: Search },
  { title: 'Write and revise', description: 'Draft, edit, and improve your writing. Get clearer, stronger versions and useful feedback.', kind: 'writing', icon: PenLine },
  { title: 'Code and debug', description: 'Build, debug, and explain code in plain language across multiple languages.', kind: 'code', icon: Code2 },
  { title: 'Learn anything', description: 'Get clear, step-by-step explanations on complex topics, from everyday questions to advanced concepts.', kind: 'learning', icon: Lightbulb },
];

function ToolIllustration({ kind, icon: Icon }: { kind: string; icon: LucideIcon }) {
  return <div className={`tool-art art-${kind}`} aria-hidden="true">
    <span className="art-orbit"/><span className="art-dot dot-one"/><span className="art-dot dot-two"/>
    <span className="art-layer"/>
    <span className="art-paper"><i/><i/><i/></span>
    <span className="art-symbol"><Icon size={kind === 'learning' ? 54 : 42} strokeWidth={1.8}/></span>
  </div>;
}

function PowerfulTools() {
  return <section className="tools-section" id="product" aria-labelledby="tools-heading" data-testid="section-tools">
    <div className="container-wide">
      <div className="section-intro"><div><span className="eyebrow">GET MORE DONE</span><h2 id="tools-heading">Powerful tools for real work.</h2></div><p>A private, flexible workspace for research, writing, coding, and learning—with real-time information when you need it.</p></div>
      <div className="tools-grid">{tools.map(tool => <article className="tool-card" key={tool.kind}><ToolIllustration kind={tool.kind} icon={tool.icon}/><h3>{tool.title}</h3><p>{tool.description}</p></article>)}</div>
    </div>
  </section>;
}

function PrivacyPromise() {
  const promises = [
    { title: 'No account required', icon: UserRound, text: 'Start a session without an account. The current product does not offer permanent account chat history.' },
    { title: 'Temporary sessions', icon: Clock, text: 'Your active conversation uses temporary server memory for context, including the facts you ask Kalillac to remember.' },
    { title: 'Clear provider disclosure', icon: ShieldCheck, text: 'OpenAI generates model answers. Relevant query text goes to the search provider used when search is needed.' },
    { title: 'Session inaccessible after refresh', icon: CircleSlash, text: "Refreshing or leaving ends this browser's access. It does not immediately delete the server-memory entry." },
  ];
  return <section className="promise-section" aria-labelledby="promise-heading" data-testid="section-promise"><div className="container-wide promise-panel">
    <span className="eyebrow">OUR PRIVACY PROMISE</span><h2 id="promise-heading">Your conversation stays temporary.</h2>
    <p className="promise-qualification">This refers to facts you ask Kalillac to remember. Current conversation context is also held temporarily; this is not a promise of complete anonymity or no third-party processing.</p>
    <div className="promise-grid">{promises.map(({ title, icon: Icon, text }) => <div className="promise-item" key={title}><span className="promise-icon" aria-hidden="true"><Icon size={25}/></span><div><h3>{title}</h3><p>{text}</p></div></div>)}</div>
  </div></section>;
}

function PrivacyFlow() {
  const stages = [
    { title: '1. You ask', icon: UserRound, text: 'Bring a question, idea, or problem. No account or profile is required.' },
    { title: '2. Temporary session', icon: FileText, text: 'Kalillac keeps current conversation context in temporary server memory. There is no permanent account chat history.' },
    { title: '3. Answer or search', icon: Sparkles, text: 'OpenAI generates model answers. Brave is primary search; Tavily may provide one bounded fallback or native evidence follow-up when eligible.' },
    { title: '4. You move on', icon: CircleSlash, text: "Refreshing or leaving ends this browser's access to the conversation. That is not immediate deletion from the server." },
  ];
  return <section className="flow-section" id="how-it-works" aria-labelledby="flow-heading" data-testid="section-flow"><div className="container-wide flow-panel">
    <div className="section-intro flow-intro"><div><span className="eyebrow eyebrow-on-dark">HOW IT WORKS</span><h2 id="flow-heading">Your conversation has boundaries.</h2></div><p>Kalillac uses AI from OpenAI and can search the current web when needed. Your conversation stays in a temporary session and isn't saved to an account.</p></div>
    <ol className="flow-stages" aria-labelledby="flow-heading" data-testid="flow-summary">{stages.map(({ title, icon: Icon, text }, index) => <li className="flow-stage" data-stage={index + 1} key={title}><span className="flow-stage-icon" aria-hidden="true"><Icon size={27}/></span><div><h3>{title}</h3><p>{text}</p></div>{index < 3 && <FlowMarkers/>}</li>)}</ol>
    <div className="provider-rail"><span className="provider-badge"><Search size={21} aria-hidden="true"/><span><strong>Brave</strong><small>Primary search provider</small></span></span><ArrowRight size={20} aria-hidden="true"/><span className="provider-badge"><Sparkles size={21} aria-hidden="true"/><span><strong>Tavily</strong><small>Fallback or follow-up when eligible</small></span></span></div>
    <p className="flow-policy">Search is used only when needed. A usable Brave response does not automatically trigger Tavily fallback. Tavily may supply one bounded fallback after an eligible Brave failure or, in native routing, one later model-requested evidence follow-up after insufficient Brave results, if Tavily has not already been attempted. Search fallback is not model fallback; there is no automatic alternate generation model. Retrieved evidence can be supplied to OpenAI for the answer.</p>
    <p className="flow-note" data-testid="flow-memory-note"><strong>Temporary session data can remain in server memory until capacity limits or a restart clear it.</strong>{' '}Ending this browser's access is not the same as deleting it from the server.</p>
  </div></section>;
}

type TranscriptTurn = { speaker: 'you' | 'kalillac'; text: string };

/* One real Kalillac session, shown in two phases. Text is verbatim from the
   session; the final response is excerpted (see the disclosure below). */
const MEMORY_SAME_SESSION: TranscriptTurn[] = [
  { speaker: 'you', text: 'Remember for me that my business name is Kalillac AI.' },
  { speaker: 'kalillac', text: 'I’ll remember that your business name is Kalillac AI for this conversation.' },
  { speaker: 'you', text: 'What is my business name?' },
  { speaker: 'kalillac', text: 'Your business name is Kalillac AI.' },
];

const MEMORY_AFTER_REFRESH: TranscriptTurn[] = [
  { speaker: 'you', text: 'Do you remember me telling you what my business name was?' },
  { speaker: 'kalillac', text: 'No. I don’t see your business name in the current session, so I can’t recover it.' },
];


function Transcript({ turns, labelledBy }: { turns: TranscriptTurn[]; labelledBy: string }) {
  return <ol className="memory-transcript" aria-labelledby={labelledBy}>{turns.map((turn, index) => <li key={index} className={`memory-msg memory-msg-${turn.speaker}`}>
    {turn.speaker === 'kalillac' && <span className="memory-avatar" aria-hidden="true"><MessageSquare size={20}/></span>}
    <div className="memory-bubble"><span className="memory-speaker">{turn.speaker === 'you' ? 'You' : 'Kalillac'}<span className="sr-only">:</span></span><p>{turn.text}</p></div>
  </li>)}</ol>;
}

function MemoryDemonstration() {
  return <section className="demos-section" id="temporary-memory" aria-labelledby="memory-heading" data-testid="section-demos"><div className="container-wide">
    <div className="section-intro demos-intro"><div><span className="eyebrow">SEE IT IN ACTION</span><h2 id="memory-heading">Temporary memory you can test yourself.</h2></div><p>Kalillac can remember information during your active conversation. Refreshing starts a new browser session that cannot reopen the previous conversation.</p></div>
    <ol className="memory-phases"><li className="memory-phase memory-phase-during" data-testid="memory-phase-during"><h3 className="memory-phase-label" id="memory-phase-during-label"><span aria-hidden="true">1</span> DURING THE SAME SESSION</h3><Transcript turns={MEMORY_SAME_SESSION} labelledBy="memory-phase-during-label"/></li><li className="memory-phase memory-phase-after" data-testid="memory-phase-after"><h3 className="memory-phase-label" id="memory-phase-after-label"><span aria-hidden="true">2</span> AFTER REFRESHING</h3><Transcript turns={MEMORY_AFTER_REFRESH} labelledBy="memory-phase-after-label"/></li></ol>
    <p className="memory-explain" data-testid="memory-explain">Refreshing ends that browser’s access to the previous temporary conversation. The old session data may remain temporarily in server memory until capacity limits or a restart clear it, but the refreshed browser cannot retrieve it.</p>
    <p className="memory-disclosure" data-testid="memory-disclosure">Real Kalillac session example. The final response was excerpted, and formatting was condensed for display.</p>
  </div></section>;
}

function SessionExplanation() {
  return (
    <section className="explain-section" id="privacy" aria-labelledby="explain-heading" data-testid="section-explain">
      <div className="container-wide">
        <h2 id="explain-heading" className="sr-only">Your session and providers in plain language</h2>
        <div className="explain-grid">
          <div className="explain-block" data-testid="explain-session">
            <span className="explain-icon" aria-hidden="true"><Clock size={24}/></span><div><h3>Your temporary session</h3>
            <p>Kalillac doesn’t require an account and doesn’t offer permanent chat history. Your conversation lasts for the active temporary session. Refreshing or leaving the page ends your browser’s access to it. Temporary session data can remain in server memory until capacity limits or a restart clear it.</p></div>
          </div>
          <div className="explain-block" data-testid="explain-providers">
            <span className="explain-icon" aria-hidden="true"><ShieldCheck size={24}/></span><div><h3>Who processes what</h3>
            <p>OpenAI generates model answers. Brave is the primary search provider when search is needed. Tavily may supply one bounded fallback after an eligible Brave failure or, in native routing, one later model-requested evidence follow-up after insufficient Brave results, if Tavily has not already been attempted. A usable Brave response does not automatically trigger Tavily fallback. Search fallback is not model fallback; there is no automatic alternate generation model. Relevant query text goes to whichever search provider is used; retrieved evidence can be supplied to OpenAI for the answer. OpenAI, Brave and Tavily process what they receive under their own policies.</p></div>
          </div>
        </div>
        <Link href="/privacy" className="text-link explain-link" data-testid="link-full-privacy">Read the full Privacy page <ArrowRight size={16} aria-hidden="true" /></Link>
      </div>
    </section>
  );
}


function MobileAppSection() {
  return <section className="mobile-app-section" aria-labelledby="mobile-app-title" data-testid="section-mobile-app"><div className="container-wide mobile-app-panel">
    <div className="app-copy"><span className="eyebrow">COMING SOON</span><h2 id="mobile-app-title">Kalillac AI app in development.</h2><p>Take Kalillac with you. A native mobile app is in progress, bringing the same private, flexible experience to your phone.</p></div>
    <div className="development-badge"><span aria-hidden="true"><Smartphone size={30}/></span><div><strong>Mobile app in progress</strong><p>No release date announced.</p></div></div>
    <div className="phone-stage" aria-hidden="true"><span className="phone-halo"/><div className="phone"><span className="phone-island"/><div className="phone-content"><img src={brandLogoUrl} alt="" className="phone-logo"/><p>Private by design.<br/>On the go.</p><span className="phone-home"/></div></div></div>
  </div></section>;
}

function HomePage() {
  const slotRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const startSession = useCallback(() => focusComposer(slotRef.current, inputRef.current), []);
  useEffect(() => {
    const id = window.location.hash.slice(1);
    if (!id) return;
    if (id === 'chat') { startSession(); return; }
    document.getElementById(id)?.scrollIntoView();
  }, [startSession]);
  return <div className="site-shell"><SiteHeader/><main>
    <section className="hero" aria-labelledby="hero-title" data-testid="section-hero"><div className="hero-signal-line" aria-hidden="true"/><div className="container-wide hero-stage">
      <div className="hero-copy"><span className="eyebrow">PRIVATE BY DESIGN</span><h1 id="hero-title"><span className="h1-phrase">Think clearly.</span>{' '}<span className="h1-phrase">Write well.</span>{' '}<span className="h1-phrase">Build and debug.</span>{' '}<span className="h1-phrase h1-accent"><span>Search the</span>{' '}<span>current web.</span></span></h1><p className="hero-description">Work through difficult questions, draft and revise writing, explain and repair code, or research current information with sources—without creating an account or building a permanent chat history.</p>
        <button type="button" className="primary-link hero-action" onClick={startSession} data-testid="button-start-session">Start a private session <ArrowRight size={18} aria-hidden="true"/></button>
        <p className="hero-under-note"><span className="trust-item"><Clock size={16} aria-hidden="true"/> Temporary sessions</span><span className="trust-item"><CircleSlash size={16} aria-hidden="true"/> No account required</span><Link href="/privacy" className="note-link trust-item"><ShieldCheck size={16} aria-hidden="true"/> Clear provider disclosure</Link></p>
      </div><div className="hero-workspace"><OrbitalAssistant/><HomeComposer slotRef={slotRef} inputRef={inputRef}/></div>
    </div></section>
    <PowerfulTools/><PrivacyPromise/><PrivacyFlow/><MemoryDemonstration/><SessionExplanation/><MobileAppSection/>
  </main><SiteFooter/></div>;
}

type DocSectionProps = { id: string; title: string; children: ReactNode };
function DocSection({ id, title, children }: DocSectionProps) {
  return <section className="doc-section" id={id}><h2>{title}</h2>{children}</section>;
}

/* The section being read: the first one inside the band just below the
   sticky header. It stays on the last section seen while the reader is
   between two, so the marker never flickers off. */
function useCurrentSection(toc: [string, string][]) {
  const [current, setCurrent] = useState<string | null>(null);
  useEffect(() => {
    if (typeof IntersectionObserver !== 'function') return;
    const visible = new Set<string>();
    const observer = new IntersectionObserver(entries => {
      for (const entry of entries) {
        if (entry.isIntersecting) visible.add(entry.target.id);
        else visible.delete(entry.target.id);
      }
      const first = toc.find(([id]) => visible.has(id));
      if (first) setCurrent(first[0]);
    }, { rootMargin: '-96px 0px -55% 0px' });
    for (const [id] of toc) {
      const section = document.getElementById(id);
      if (section) observer.observe(section);
    }
    return () => observer.disconnect();
  }, [toc]);
  return current;
}

function DocLayout({ title, lede, children, toc, updated }: { title: string; lede: ReactNode; children: ReactNode; toc: [string, string][]; updated: string }) {
  const current = useCurrentSection(toc);
  return (
    <div className="site-shell">
      <SiteHeader />
      <main className="doc">
        <div className="doc-shell">
          <div className="doc-head">
            <Link href="/" className="doc-home" data-testid="link-doc-home">← Home</Link>
            <h1 className="doc-title">{title}</h1>
            {lede}
          </div>
          <nav className="doc-toc" aria-label="On this page"><h2>On this page</h2><ol>{toc.map(([id, label]) => <li key={id}><a href={`#${id}`} aria-current={current === id ? 'location' : undefined}>{label}</a></li>)}</ol></nav>
          <div className="doc-body">
            {children}
            <p className="doc-updated">{updated}</p>
          </div>
        </div>
      </main>
      <SiteFooter />
    </div>
  );
}

const privacyToc: [string, string][] = [
  ['overview', 'Information Kalillac processes'], ['not-persisted', 'What Kalillac does not save'], ['session-state', 'Temporary session state'], ['isolation', 'Session isolation'], ['browser', 'Browser storage'], ['visitor-cookie', 'Daily allowance cookie'], ['logging', 'Logging'], ['model-inference', 'Model inference: OpenAI'], ['tavily', 'Web search: Brave and Tavily'], ['infrastructure', 'Network infrastructure: Cloudflare'], ['cloudflare-security', 'Cloudflare security checks'], ['analytics', 'Analytics and performance measurement'], ['path', 'How a message moves'], ['security', 'Security'], ['age', 'Age'], ['retention', 'Retention in brief'], ['choices', 'What you can do'], ['changes', 'Changes'], ['source', 'About this page'], ['contact', 'Contact'],
];

function PrivacyPage() {
  return <DocLayout title="Privacy" toc={privacyToc} updated="Last updated: November 6, 2026" lede={<><p className="doc-lede">This page describes how information is handled when you use Kalillac AI at <span className="mono">kalillac.ai</span>, including the chat application.</p><p className="doc-lede">Kalillac is an AI assistant for questions, research, writing, coding, learning, and answers that may use the current web. No account or paid subscription is currently offered. This page describes the service as it is designed now. It is not a promise that the service is anonymous, that data is never stored at any layer, or that the service will never change.</p></>}>
    <DocSection id="overview" title="Information Kalillac processes"><p>To answer a message, the application handles:</p><ul><li><strong>The message you type.</strong> Currently limited to 4,000 characters.</li><li><strong>Recent turns of the open conversation.</strong> The chat interface holds the current thread in page memory and sends recent turns with each request so the assistant has context.</li><li><strong>A session identifier.</strong> The server generates an opaque identifier and uses it as the key for a temporary session entry. The chat interface keeps that identifier in page memory. It is not an account.</li><li><strong>Facts you explicitly ask Kalillac to remember during the session.</strong> Those facts are stored as short entries in that temporary session entry.</li><li><strong>Timestamps of recent web searches in the session.</strong> Used only to enforce the current limit of five searches per ten minutes.</li><li><strong>A daily allowance cookie, when the anonymous daily allowance is active.</strong> A random visitor value used only to apply the free daily limit, as described in <a href="#visitor-cookie">Daily allowance cookie</a>.</li></ul><p>Kalillac does not ask for your name, email address, phone number, or account details. The current service has no file upload and does not read documents from your device.</p><p>The systems that deliver the site, including the web server, the operating system, the hosting provider, and Cloudflare, also process ordinary request data such as IP address, date and time, and user agent.</p></DocSection>
    <DocSection id="not-persisted" title="What Kalillac does not save"><p>Kalillac does not save chat transcripts as persistent conversations in its own application database. In its own application, Kalillac does not:</p><ul><li>store chat history across visits</li><li>keep a user profile tied to you</li><li>keep search results in a Kalillac database after using them to write an answer</li></ul><p>Those statements describe Kalillac’s own application. They do not mean that no copy, log, or metadata can exist anywhere else. OpenAI, Brave, Tavily, Cloudflare, and DigitalOcean handle what they receive under their own policies.</p></DocSection>
    <DocSection id="session-state" title="Temporary session state"><p>Kalillac’s session state is an entry in the memory of the running application process. It is not written to a database.</p><p>The entry holds two things:</p><ol><li>memory facts from that session</li><li>timestamps of recent searches in that session</li></ol><p>The application keeps at most 200 session entries at once and at most 50 memory entries per session. When a new session arrives and the store is full, the oldest entry is removed.</p><h3>How this state ends</h3><ul><li>Refreshing or closing the page removes the browser’s access to that session. The chat interface does not keep the thread in saved browser storage, and a new page starts a new, empty session that cannot read the previous entry.</li><li>That does not promise immediate deletion of the server-memory entry. Active-session context may remain in server memory until eviction or service restart.</li><li>The chat’s <strong>End &amp; erase</strong> control ends a conversation on demand. It clears the conversation, the session identifier and any unsent draft from the page, and asks Kalillac to remove that session’s temporary server-memory entry. When that request reaches Kalillac, it removes the entry it holds for that session and asks any response still being prepared for it to stop. If the page cannot confirm this, it says so and offers to retry. Stopping is best effort: an AI or search provider may keep processing what it already received.</li><li>End &amp; erase does not retract information already sent to OpenAI, Brave or Tavily. It does not erase daily allowance records, account or billing records, or operational and security logs kept by Kalillac, Cloudflare or DigitalOcean, and it is not a secure wipe of server memory.</li><li>When the application process restarts, the in-memory store is gone, because it existed only in that process.</li></ul></DocSection>
    <DocSection id="isolation" title="Session isolation"><p>Each session is stored under its own identifier. A request looks up only the matching entry. There is no shared conversation buffer and no cross-session lookup, so one visitor’s memory entries are not read into another visitor’s context.</p><p>Capacity is shared. With a limit of 200 sessions, heavy traffic can evict older sessions sooner. That affects when state disappears, not who can read it.</p></DocSection>
    <DocSection id="browser" title="Browser storage"><p>The chat interface keeps the session identifier and the completed turns of the open conversation in page memory only. It does not write them to localStorage, sessionStorage, IndexedDB, cookies, or the page address.</p><p>When the anonymous daily allowance is active, Kalillac’s chat service also sets the <a href="#visitor-cookie">daily allowance cookie</a> described below. That cookie holds a random visitor value and none of your conversation.</p><p>The homepage has its own message box and does not load the chat application. Typing there sends nothing. When you press Send, the homepage places that one message in an envelope in this browser tab’s sessionStorage and opens the chat application at <span className="mono">/app/</span>, which normally removes the envelope when it reads it and then sends the message as the first message of a new temporary session, under the same temporary session behavior described above. A message the chat application does not pick up within a few seconds is not sent automatically. In rare cases, when this browser’s storage fails or it cannot be confirmed which page has the message, the envelope may remain in that tab’s sessionStorage for the life of the tab, so that the message is neither lost nor sent twice. It is not saved to an account or sent anywhere because of this. So that the same message is not sent twice, the chat application also keeps a bounded list of recently used random identifiers in that tab’s sessionStorage. The list contains no prompt or message content and may remain in the tab after the message is sent. Your messages and Kalillac’s responses are not passed through the page address, cookies, localStorage, or messages between browser windows.</p><p>Kalillac’s own frontend uses your device’s fonts and does not include advertising code, browser analytics code, remote fonts, or third-party asset dependencies. Cloudflare may separately add the <a href="#cloudflare-security">security code described below</a> at its edge.</p></DocSection>
    <DocSection id="visitor-cookie" title="Daily allowance cookie"><p>The anonymous daily allowance is active in the current service.</p><p>When the anonymous daily allowance is active, Kalillac uses one strictly necessary first-party cookie, named <span className="mono">__Host-kalillac_visitor</span>, to apply the free allowance of 10 successful responses per browser per UTC day. It contains a random, opaque visitor value. It does not contain your messages, Kalillac’s responses, or the session identifier.</p><p>The chat service sets this cookie with HttpOnly, Secure, SameSite=Lax, Path=/ and no Domain attribute. It is sent only to the Kalillac host that set it and cannot be read by page scripts. Each anonymous chat response refreshes its 48-hour lifetime.</p><p>Kalillac does not store the raw visitor value in its PostgreSQL database. For each UTC day, the server derives a SHA-256 digest from the visitor value and date. The stored digest changes each UTC day and does not itself provide a stable identifier across days. The database also stores the reservation state and timestamps needed to enforce or refund the allowance.</p><p>The service is designed to retain the current and previous UTC day’s records and lazily prune older records during later anonymous traffic. Because pruning is lazy and best-effort, older records may remain longer when there is no anonymous traffic or cleanup cannot complete.</p><p>The allowance does not use an IP address, browser fingerprinting or account identity. Kalillac does not use this cookie for advertising, analytics or cross-site tracking.</p><p>Clearing or refusing this cookie causes the browser to receive a new visitor value and can reset its per-browser allowance.</p></DocSection>
    <DocSection id="logging" title="Logging"><p>The application writes operational output used to run and diagnose the service. Kalillac does not use that output as a conversation archive. This page does not claim that message content can never appear in server logs.</p><p>The web server, the operating system, Cloudflare, and the hosting provider can create their own request, operational, and security records, which can include data such as IP address, time, and user agent. Those records are governed by the policies of the systems and providers that create them.</p></DocSection>
    <DocSection id="model-inference" title="Model inference: OpenAI"><p>OpenAI may process requests sent for model inference. Kalillac does not run its model on the Kalillac server. The current production model setting is GPT-6 Luna. OpenAI is Kalillac’s only model provider, and there is no automatic fallback to another model or provider: if OpenAI cannot return a usable answer, the request ends with an error instead of being sent elsewhere.</p><p>Some requests never go to OpenAI. For example, deterministic arithmetic and some temporary session-memory requests are handled by Kalillac’s own code.</p><p>A request sent to OpenAI can include, as applicable:</p><ul><li>your current message</li><li>relevant recent turns</li><li>relevant temporary session-memory entries</li><li>the instructions Kalillac assembles for the request</li><li>retrieved web-search text, when the answer uses a search</li></ul><p>Cloudflare Workers AI and Groq are not part of the current active model-provider path.</p><p>OpenAI handles what it receives under its own terms and policies, which Kalillac does not control: <a href="https://openai.com/policies/" target="_blank" rel="noopener noreferrer">OpenAI policies</a>.</p></DocSection>
    <DocSection id="tavily" title="Web search: Brave and Tavily"><p>When web search is used, relevant query text may be sent to Brave or Tavily, depending on which provider is used. Not every message uses web search. OpenAI generates model answers; neither search provider writes Kalillac's answers.</p><p>Brave Search is the primary web search provider. Tavily may provide a bounded fallback when Brave cannot provide usable evidence, or follow-up searches to clarify or verify insufficient evidence. Search fallback and verification do not change the model provider: OpenAI remains the only model provider. A search may fail or leave information unverified.</p><p>When a search runs:</p><ul><li>Kalillac builds the query from the current request. For a follow-up question, the query can also include the earlier topic needed to understand it. The query is limited to 400 characters.</li><li>Search providers return titles, links, and text snippets that Kalillac uses as evidence.</li><li>Kalillac uses the retrieved evidence for the response. When the model writes the answer, that evidence can be sent to OpenAI as described above.</li><li>Kalillac does not keep those results in a Kalillac database.</li><li>Each session is limited to five lookups in a ten-minute window; a bounded fallback or follow-up shares that lookup's session allowance.</li></ul><p>Brave and Tavily handle what they receive under their own terms and privacy policies, which Kalillac does not control. Their retention, logging, storage and training practices are not established by Kalillac's application design. Tavily's policy is available here: <a href="https://www.tavily.com/privacy" target="_blank" rel="noopener noreferrer">Tavily Privacy Policy</a>.</p></DocSection>
    <DocSection id="infrastructure" title="Network infrastructure: Cloudflare"><p>Cloudflare provides reverse proxy, CDN, security and DNS services for <span className="mono">kalillac.ai</span>. Cloudflare is not a Kalillac model provider.</p><p>Traffic to <span className="mono">kalillac.ai</span> passes through Cloudflare before reaching Kalillac’s server. Cloudflare terminates the browser-facing HTTPS connection and forwards the request to Kalillac’s origin over a separate encrypted connection. This means Cloudflare can process request content while proxying it, including chat requests, along with ordinary network information such as IP address, request headers, requested path, browser information, and time. Cloudflare handles what it receives under its own policies.</p><p>DigitalOcean hosts Kalillac’s origin server and handles traffic and system data under its own policies.</p><p>Kalillac does not control Cloudflare’s policies: <a href="https://www.cloudflare.com/privacypolicy/" target="_blank" rel="noopener noreferrer">Cloudflare Privacy Policy</a>.</p></DocSection>
    <DocSection id="cloudflare-security" title="Cloudflare security checks"><p>Cloudflare may add security code used to detect automated or abusive traffic. When those checks run, Cloudflare may set an HttpOnly <span className="mono">cf_clearance</span> cookie showing that the browser passed a security check. Kalillac does not use this cookie to store, reconstruct, or identify prompts, responses, or conversation history.</p></DocSection>
    <DocSection id="analytics" title="Analytics and performance measurement"><p>Cloudflare Web Analytics and Real User Measurements are disabled for <span className="mono">kalillac.ai</span>. Kalillac does not load Cloudflare’s browser RUM beacon or send prompts, responses, or conversation content to that beacon.</p><p>Cloudflare still produces traffic, operational, performance, and security information from requests handled at its edge. Those infrastructure records and aggregate statistics are separate from the disabled browser RUM beacon and are handled under Cloudflare’s own policies.</p></DocSection>
    <DocSection id="path" title="How a message moves"><p>The public site and the chat application are served from the same domain. A chat request passes through Cloudflare’s network to the Kalillac server, where the application decides how to handle it.</p><ul><li><strong>Handled by Kalillac.</strong> For example, deterministic arithmetic or some temporary session-memory requests, without OpenAI, Brave or Tavily.</li><li><strong>Model response.</strong> The request goes to OpenAI.</li><li><strong>Web search.</strong> Relevant query text goes to whichever search provider is used: Brave Search is primary, and Tavily may supply fallback or verification searches as described above. Retrieved evidence can go to OpenAI when the model writes the answer. The response can include source links.</li></ul></DocSection>
    <DocSection id="security" title="Security"><p>No online service can promise that it is completely secure, or that no person or provider could ever access a request, a log, or provider-side data. Do not send passwords, API keys, credentials, payment-card numbers, or other highly sensitive secrets. Ordinary use of Kalillac does not require them.</p></DocSection>
    <DocSection id="age" title="Age"><p>Kalillac is for adults. You must be at least 18. The service is not directed to children. Kalillac does not knowingly seek information from anyone under 18. If it becomes aware that someone under 18 is using the service, it may block that access.</p><p>Use of the service is a representation that you are at least 18. Kalillac does not run a separate age-verification process.</p></DocSection>
    <DocSection id="retention" title="Retention in brief"><table><thead><tr><th>Information</th><th>Where it lives now</th><th>When it ends</th></tr></thead><tbody><tr><td>Open conversation in the chat interface</td><td>Browser page memory</td><td>The browser loses access on refresh, closing the page, or leaving it. End &amp; erase clears it from the page</td></tr><tr><td>Session identifier</td><td>Page memory, as the key for the server entry</td><td>Same as that page session</td></tr><tr><td>Memory facts and search timestamps</td><td>Server memory, up to 200 sessions and 50 facts</td><td>End &amp; erase, eviction or service restart. Refreshing or closing the page does not promise immediate deletion</td></tr><tr><td>Chat transcripts</td><td>Not saved as persistent conversations in Kalillac’s application database</td><td>Not kept as chat history</td></tr><tr><td>Daily allowance cookie, when the anonymous daily allowance is active</td><td>Your browser</td><td>48 hours after the last anonymous chat response that refreshed it, or when you clear it</td></tr><tr><td>Daily allowance records: a per-day digest, reservation state and timestamps</td><td>Kalillac’s application database</td><td>Designed to keep the current and previous UTC day. Older records are pruned lazily during later anonymous traffic and may remain longer</td></tr><tr><td>OpenAI</td><td>OpenAI’s systems</td><td>OpenAI’s policies</td></tr><tr><td>Brave or Tavily, when used for search</td><td>The search provider's systems</td><td>That provider's policies</td></tr><tr><td>Cloudflare and DigitalOcean hosting records</td><td>Those providers</td><td>Their policies</td></tr></tbody></table><p>There is no Kalillac account and no Kalillac conversation archive to export or delete. That is not a claim that no record exists at a provider, or that Kalillac can find and erase every infrastructure record on request.</p></DocSection>
    <DocSection id="choices" title="What you can do"><p>You choose what to type. Do not submit information you cannot allow OpenAI, Brave, Tavily, Cloudflare, or DigitalOcean to process. You can avoid questions that need web search if you do not want query text sent to Brave or Tavily. The service does not ask for an email address and has no account settings.</p><p>When the anonymous daily allowance is active, you can clear or block its cookie in your browser. Doing so can reset your browser’s daily allowance.</p><p>You can use End &amp; erase in the chat to end a conversation and have Kalillac remove its temporary session memory for it. <a href="#session-state">Temporary session state</a> explains what it does not erase.</p></DocSection>
    <DocSection id="changes" title="Changes"><p>Kalillac is under active development. Models, providers, limits, and session settings are operational choices, not permanent commitments. Possible later features, such as optional accounts or paid plans, are not part of the current service.</p><p>If message handling changes in a way that matters, this page will be updated to describe the new behavior.</p></DocSection>
    <DocSection id="source" title="About this page"><p>Statements about Kalillac’s own application describe its source code and design. Statements about OpenAI, Brave, Tavily, and Cloudflare are limited to what Kalillac sends them; their own policies govern what they do with it. This page is an operator description. It is not an independent privacy or security audit.</p></DocSection>
    <DocSection id="contact" title="Contact"><p>Questions and corrections can go to Kalillac AI through <a href="https://www.linkedin.com/in/kalillacai" target="_blank" rel="noopener noreferrer">LinkedIn</a>.</p></DocSection>
  </DocLayout>;
}

const termsToc: [string, string][] = [
  ['service', 'The service'], ['age', 'Who may use it'], ['support', 'No account and no paid subscription'], ['ai-limitations', 'AI responses'], ['responsibilities', 'Your responsibilities'], ['user-actions', 'Your searches and later actions'], ['boundaries', 'Acceptable use'], ['privacy', 'Privacy and other companies'], ['content', 'Prompts and output'], ['intellectual-property', 'Kalillac intellectual property'], ['availability', 'Availability and access'], ['warranties', 'Disclaimers and liability'], ['law', 'Governing law'], ['changes', 'Changes to these Terms'], ['contact', 'Contact'],
];

function TermsPage() {
  return <DocLayout title="Terms of Use" toc={termsToc} updated="Last updated: October 27, 2026" lede={<><p className="doc-lede">These Terms govern use of Kalillac AI, the service at <span className="mono">kalillac.ai</span>. Kalillac is an AI assistant for questions, research, writing, coding, learning, and answers that may use the current web.</p><p className="doc-lede">The service is currently free and does not require an account. It is only for adults age 18 and older. By using Kalillac, you agree to these Terms. If you do not agree, do not use the service.</p></>}>
    <DocSection id="service" title="The service"><p>Some requests are handled by Kalillac’s own code. OpenAI may process requests sent for model inference. When web search is used, relevant query text may be sent to Brave or Tavily, depending on which provider is used. Brave is primary; Tavily can supply the single bounded fallback or native evidence follow-up described on the Privacy page. Retrieved evidence can be supplied to OpenAI for the answer. OpenAI remains the only generation-model provider, with no automatic alternate generation model. Cloudflare provides reverse proxy, CDN, security and DNS services for the site. Cloudflare Workers AI and Groq are not part of the current active model-provider path. Current data handling is described on the <Link href="/privacy">Privacy page</Link>.</p><p>The service is under active development. Features, models, providers, limits, routing, and the interface may change. No particular model, provider, search capability, limit, or screen is guaranteed to remain available.</p></DocSection>
    <DocSection id="age" title="Who may use it"><p>You must be at least <strong>18 years old</strong>. Kalillac is not directed to children or minors. If you are under 18, do not use it.</p><p>By using Kalillac, you represent that you are at least 18. Kalillac may restrict access if it becomes aware that someone under 18 is using the service. These Terms rely on your representation. They do not describe a separate age-verification process.</p></DocSection>
    <DocSection id="support" title="No account and no paid subscription"><p>The free allowance is 10 successful responses per browser per UTC day. The service uses the host-only <span className="mono">__Host-kalillac_visitor</span> cookie and PostgreSQL usage records containing a daily SHA-256 digest, reservation state and timestamps, rather than the raw visitor value or persistent conversation history. See the <Link href="/privacy#visitor-cookie">Privacy page’s daily allowance disclosure</Link> for cookie settings and retention.</p><p>No account or paid subscription is currently offered. These Terms are posted on the site because there is no registration step.</p><p>A voluntary <a href="https://buymeacoffee.com/kalillactv7" target="_blank" rel="noopener noreferrer">Buy Me a Coffee</a> link is available if you want to support development and infrastructure. That payment is not a subscription. By itself, it does not buy priority access, guaranteed uptime, ownership, extra features, or a permanent right to use the service.</p><p>Kalillac may later offer paid features or optional accounts. If it does, those terms will be stated separately. Nothing here means a paid plan exists now.</p></DocSection>
    <DocSection id="ai-limitations" title="AI responses"><p>Responses can be inaccurate, incomplete, outdated, or misleading, including when they sound confident. Kalillac may misunderstand context, reason incorrectly, or cite material that still needs to be checked.</p><p>You decide whether to rely on a response. Verify information before you act on it, especially where a mistake could affect health, legal rights, money, safety, work, education, or another important decision.</p><p>Kalillac can discuss legal, medical, financial, technical, and other professional subjects. The responses are informational. Use of the service does not create an attorney-client, doctor-patient, fiduciary, therapist-client, or other professional relationship with Kalillac AI.</p><p>Where professional judgment matters, treat Kalillac as one source of information, not as a substitute for a qualified professional who can evaluate your situation.</p></DocSection>
    <DocSection id="responsibilities" title="Your responsibilities"><p>You control what you ask and what you do with the answer. You are responsible for your prompts, searches, and instructions, and for decisions, downloads, purchases, installations, communications, transactions, and other actions you take as a result of using the service.</p><p>You are responsible for:</p><ul><li>the content you submit</li><li>judging whether a response is accurate, lawful, safe, and suitable before you use it</li><li>how you use, share, publish, run, install, or otherwise rely on generated material</li><li>complying with laws, contracts, licenses, and other duties that apply to you</li><li>respecting other people’s rights, privacy, property, accounts, credentials, and systems</li><li>checking information when an error could have real consequences</li></ul><p>Do not submit passwords, credentials, API keys, financial account numbers, or other highly sensitive information the request does not require, or information you cannot allow OpenAI, Brave, Tavily, Cloudflare, or DigitalOcean to process.</p></DocSection>
    <DocSection id="user-actions" title="Your searches and later actions"><p>Kalillac provides information. It does not control what you search, ask, investigate, open, download, install, buy, publish, execute, send, or do after a response.</p><p>You are responsible for whether you act, and for the consequences. That includes consequences involving other websites, software, services, accounts, transactions, devices, files, people, or systems you choose to interact with.</p><p>Kalillac AI does not authorize, direct, endorse, or take responsibility for your independent conduct merely because the service discussed a subject, returned information, linked a source, or generated related text.</p><p>To the fullest extent permitted by law, you assume the risk of actions you independently choose to take based on, or after, using Kalillac. This section does not remove a responsibility the law does not allow to be removed.</p><div className="doc-callout doc-callout-amber">A response is information. It is not permission to interfere with another person’s rights, property, privacy, accounts, credentials, or computer systems.</div></DocSection>
    <DocSection id="boundaries" title="Acceptable use"><p>Kalillac is meant to engage with difficult, controversial, and technically sensitive subjects. A subject is not forbidden merely because it is uncomfortable, or because someone else’s terms of service, game rules, or competition rules would restrict it. Those outside rules do not automatically become Kalillac’s rules.</p><p>Kalillac may refuse or narrow a request when the output would materially help cause serious real-world harm to a person, or to that person’s property, finances, privacy, credentials, or computer systems, or when providing it would conflict with law or with a binding requirement on the service.</p><p>If only part of a request has that problem, the intended behavior is to limit that part and continue with the rest, rather than refuse the whole conversation. That is the intended design. It is not a warranty that every answer will be divided perfectly.</p><p>You may not use Kalillac to intentionally facilitate serious harm, including fraud, credential theft, exploitation of children, destructive compromise of systems you do not control and are not authorized to test, or planning real-world violence.</p></DocSection>
    <DocSection id="privacy" title="Privacy and other companies"><p>OpenAI is the only model provider, with GPT-6 Luna as the current production model setting and no automatic model-provider fallback. Brave Search is primary for web search; Tavily may provide fallback or verification. Cloudflare provides the reverse proxy, CDN, security and DNS services, and DigitalOcean hosts the origin server.</p><p>The <Link href="/privacy">Privacy page</Link> describes messages, session state, logging, and providers. OpenAI, Brave, Tavily, Cloudflare, and DigitalOcean operate under their own terms. Kalillac does not control them and does not promise anything on their behalf.</p></DocSection>
    <DocSection id="content" title="Prompts and output"><p>As between you and Kalillac, Kalillac does not claim ownership of the text you submit. You give Kalillac the limited permission required to process that content so it can respond and operate the service as the Privacy page describes.</p><p>Kalillac does not promise that output is unique, copyrightable, accurate, non-infringing, or suitable for commercial use. Other people may receive similar or identical output. Rights in AI-generated material depend on the law, the source material, and third-party terms.</p><p>You are responsible for having the rights you need before you publish, sell, distribute, or otherwise rely on generated material.</p></DocSection>
    <DocSection id="intellectual-property" title="Kalillac intellectual property"><p>Unless stated otherwise, rights to the Kalillac name, branding, logo, original site design, original written content, and proprietary application code are reserved or used with permission.</p><p>Open-source libraries, third-party models, third-party services, trademarks, and other third-party materials stay under their own licenses and owners.</p><p>These Terms do not transfer Kalillac’s branding, private source code, credentials, system configuration, or other proprietary materials.</p></DocSection>
    <DocSection id="availability" title="Availability and access"><p>Kalillac may be unavailable, slow, rate-limited, changed, suspended, or discontinued. Advance notice is not guaranteed.</p><p>Kalillac may limit or block access when reasonably necessary to protect the service, respond to abuse, meet a legal duty, keep the system stable, or enforce these Terms. There is no user account to close. The action is a limit on access to the service.</p></DocSection>
    <DocSection id="warranties" title="Disclaimers and liability"><p>To the fullest extent permitted by law, Kalillac is provided “as is” and “as available,” without warranties that it will be uninterrupted, error-free, secure, accurate, current, complete, or fit for a particular purpose.</p><p>You are responsible for evaluating information before you rely on it, and for what you independently choose to do. Kalillac AI is not responsible for losses, penalties, bans, account actions, damages, injuries, disputes, security incidents, legal or financial consequences, data loss, or other outcomes caused by your own conduct, or by your decision to follow, run, publish, install, buy, transmit, or otherwise act on generated information, except where the law imposes a responsibility that cannot be disclaimed.</p><p>To the fullest extent permitted by law, Kalillac AI is not liable for indirect, incidental, special, consequential, exemplary, or similar losses arising from use of the service, inability to use it, reliance on it, interactions with third-party sites, services, software, or content, or actions taken independently after using Kalillac.</p><p>These Terms do not exclude or limit liability that cannot lawfully be excluded or limited. Mandatory consumer rights still apply where they apply.</p></DocSection>
    <DocSection id="law" title="Governing law"><p>These Terms are governed by the laws of the State of Indiana, without regard to conflict-of-law rules, except where federal law or a mandatory consumer protection says otherwise.</p><p>These Terms do not waive a right that applicable law does not allow you to waive.</p></DocSection>
    <DocSection id="changes" title="Changes to these Terms"><p>Kalillac may update these Terms. The new version will be posted here with a revised date.</p><p>A material change should be stated openly. A material change in privacy or data handling should also be reflected on the Privacy page, so the public description matches the service that is running.</p><p>Continued use after updated Terms are posted means those Terms apply to later use, subject to applicable law.</p></DocSection>
    <DocSection id="contact" title="Contact"><p>Questions about these Terms can be sent to Kalillac AI through <a href="https://www.linkedin.com/in/kalillacai" target="_blank" rel="noopener noreferrer">LinkedIn</a>.</p></DocSection>
  </DocLayout>;
}

function FramedNotice() {
  return <main className="not-found"><p>This page cannot be displayed inside another page.</p></main>;
}

function AccountShell() {
  return (
    <div className="site-shell account-shell">
      <SiteHeader />
      <Suspense fallback={<main className="account-loading" aria-busy="true" style={{ minHeight: '60vh' }} />}>
        <AccountPages />
      </Suspense>
      <SiteFooter />
    </div>
  );
}

function Router() {
  const [location] = useLocation();
  const isAccount = isAccountPath(location);
  useEffect(() => {
    if (isAccount) updateAccountMetadata(location);
    else updatePublicMetadata(location);
  }, [location, isAccount]);

  const isHome = location === '/';

  return (
    <ErrorBoundary resetKey={location}>
      {isHome ? <HomePage /> : isAccount ? <AccountShell /> : (
        <Switch>
          <Route path="/privacy" component={PrivacyPage} />
          <Route path="/terms" component={TermsPage} />
          <Route component={NotFound} />
        </Switch>
      )}
    </ErrorBoundary>
  );
}

/* Every in-app navigation (<Link> and navigate()) passes through here. The new
   page starts at its top, or at its #fragment target when the link has one,
   instead of inheriting the previous page's scroll position. The jump is
   immediate: an inline scroll-behavior briefly overrides the site's smooth
   scrolling (not the newer 'instant' value, which older browsers reject). Browser
   Back/Forward does not pass through here, so its scroll restoration is kept. */
const jumpTo = (scroll: () => void) => {
  const root = document.documentElement;
  const previous = root.style.scrollBehavior;
  root.style.scrollBehavior = 'auto';
  // Read it back so the browser applies the override before scrolling;
  // otherwise it scrolls with the stale (smooth) computed value.
  void getComputedStyle(root).scrollBehavior;
  scroll();
  root.style.scrollBehavior = previous;
};

const startNavigationAtTop: AroundNavHandler = (navigate, to, options) => {
  navigate(to, options);
  const fragment = to.includes('#') ? decodeURIComponent(to.slice(to.indexOf('#') + 1)) : '';
  if (!fragment) {
    jumpTo(() => window.scrollTo(0, 0));
    return;
  }
  // The target section belongs to the page being rendered; scroll once it exists.
  requestAnimationFrame(() => {
    const target = document.getElementById(fragment);
    jumpTo(() => (target ? target.scrollIntoView() : window.scrollTo(0, 0)));
  });
};

function App() {
  if (IS_FRAMED) return <FramedNotice />;
  return <WouterRouter aroundNav={startNavigationAtTop}><Router /></WouterRouter>;
}

export default App;
