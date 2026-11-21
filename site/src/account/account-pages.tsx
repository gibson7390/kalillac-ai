/* Account pages: sign in, create account, account overview, password recovery
   and email verification, over the existing same-origin HTTP contracts.

   Privacy boundaries kept here:
   - Password inputs are uncontrolled and read from the element on submit, so
     a password never becomes a DOM attribute or long-lived component state.
   - A recovery or verification credential arrives only as #token=, is taken
     into memory and removed from the address bar before anything else runs,
     is never rendered or logged, and is dropped once it has been used.
   - Each credential has its own generation. A response that arrives after a
     newer credential replaced it is ignored completely.
   - Nothing is written to browser storage or cookies, and the only state
     carried between pages (a notice, the email just registered) lives in
     memory for this page load. */
import { type FormEvent, type ReactNode, type RefObject, useCallback, useEffect, useId, useRef, useState } from 'react';
import { Link, Redirect, useLocation } from 'wouter';
import NotFound from '@/pages/not-found';
import * as api from './account-api';
import * as registrationApi from './registration-api';
import { ACCOUNT_PATHS } from './account-routes';
import { chatHref, registerSignOut, setPresence, takeArrival, useCustomerSession } from '@/customer-session';
import './account.css';

/* --- page-lifetime memory ---------------------------------------------------- */

// 'caution': an everyday slip the customer can simply correct (a mistyped password), shown compactly.
type NoticeData = { tone: 'error' | 'success' | 'info' | 'caution'; title: string; body?: ReactNode };

const memory: { email: string; notice: NoticeData | null } = { email: '', notice: null };

function carryNotice(notice: NoticeData) { memory.notice = notice; }
function takeNotice(): NoticeData | null {
  const notice = memory.notice;
  memory.notice = null;
  return notice;
}

const SESSION_ENDED: NoticeData = {
  tone: 'info', title: 'Your session has ended', body: 'Sign in again to continue.',
};

const SIGNED_OUT: NoticeData = {
  tone: 'info', title: 'You’re signed out',
  body: 'Temporary chat works the same whether or not you’re signed in.',
};

/* The header's Sign out (customer-session.ts) signs out through this page's own client. */
registerSignOut(async () => {
  const result = await api.logout();
  if (result.ok) carryNotice(SIGNED_OUT);
  return result.ok;
});

/* --- small helpers ------------------------------------------------------------------ */

const PASSWORD_MIN = 12;
const PASSWORD_MAX = 256;
const EMAIL_MAX = 320;
// The same check the server applies after trimming and lowercasing.
const EMAIL_SHAPE = /^[^@\s]+@[^@\s]+\.[^@\s]+$/;
// V2 mirrors the selected delivery gateway; legacy/login keep EMAIL_SHAPE.
const V2_EMAIL_SHAPE = /^[A-Za-z0-9._%+-]{1,64}@(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}$/;
// 32 random bytes in canonical unpadded URL-safe base64: 43 characters whose
// last character carries only four meaningful bits.
const TOKEN_SHAPE = /^[A-Za-z0-9_-]{42}[AEIMQUYcgkosw048]$/;

function normalizedEmail(value: string): string {
  return value.trim().toLowerCase();
}

function emailProblem(value: string): string | null {
  if (!value) return 'Enter your email address.';
  if (value.length > EMAIL_MAX || !EMAIL_SHAPE.test(value)) return 'Enter an email address like name@example.com.';
  return null;
}

// The server counts characters (code points), not UTF-16 units.
function newPasswordProblem(value: string): string | null {
  const length = Array.from(value).length;
  if (!length) return 'Enter a password.';
  if (length < PASSWORD_MIN || length > PASSWORD_MAX) return 'Use 12–256 characters.';
  return null;
}

function waitPhrase(seconds: number | null): string {
  if (!seconds) return 'Wait a few minutes, then try again.';
  const minutes = Math.ceil(seconds / 60);
  return minutes <= 1 ? 'Wait about a minute, then try again.' : `Wait about ${minutes} minutes, then try again.`;
}

const UNREACHABLE: NoticeData = {
  tone: 'error', title: 'Kalillac couldn’t be reached',
  body: 'Check your connection, then try again.',
};

function formatDate(iso: string | null | undefined, withDay: boolean): string | null {
  if (!iso) return null;
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return null;
  return new Intl.DateTimeFormat('en-US', withDay
    ? { year: 'numeric', month: 'long', day: 'numeric' }
    : { year: 'numeric', month: 'long' }).format(date);
}

/* --- fragment credentials ------------------------------------------------------- */

/* Takes #token=<credential> into memory and removes every fragment from the
   address bar at once, keeping the same history entry. Returns null for a
   missing or malformed credential; the caller shows one invalid-link state. */
function takeFragmentToken(): string | null {
  const fragment = window.location.hash;
  if (fragment) {
    window.history.replaceState(window.history.state, '', window.location.pathname + window.location.search);
  }
  if (!fragment.startsWith('#token=')) return null;
  const value = fragment.slice('#token='.length);
  return TOKEN_SHAPE.test(value) ? value : null;
}

type Credential = { token: string | null; generation: number };

/* The credential this page is working with. The fragment present on load is
   generation 1; a link opened later in the same tab changes only the
   fragment (no reload), and is taken, scrubbed and given the next generation.
   Generations, not token strings, decide whether a response is still current,
   so even the same link opened twice is two distinct credentials. Scrubbing
   in place never fires hashchange. */
function useCredential(onLaterCredential: (credential: Credential) => void) {
  const [initial] = useState(() => takeFragmentToken());
  const generation = useRef(1);
  const credential = useRef<Credential>({ token: initial, generation: 1 });
  useEffect(() => {
    const handle = () => {
      if (!window.location.hash) return;
      generation.current += 1;
      credential.current = { token: takeFragmentToken(), generation: generation.current };
      onLaterCredential(credential.current);
    };
    window.addEventListener('hashchange', handle);
    return () => window.removeEventListener('hashchange', handle);
  }, [onLaterCredential]);
  const isCurrent = useCallback((taken: Credential) =>
    credential.current.generation === taken.generation && credential.current.token === taken.token, []);
  return { initial, credential, isCurrent };
}

/* --- session -------------------------------------------------------------------------- */

type SessionState = api.SessionResult | { state: 'loading' };

function useSession() {
  const [session, setSession] = useState<SessionState>({ state: 'loading' });
  const alive = useRef(true);
  const load = useCallback(async () => {
    setSession({ state: 'loading' });
    const result = await api.getSession();
    // The header learns the answer too (memory only), so it offers a signed-in customer the way around.
    setPresence(result.state === 'signed-in' ? 'signed-in' : 'signed-out');
    if (alive.current) setSession(result);
  }, []);
  useEffect(() => {
    alive.current = true;
    void load();
    return () => { alive.current = false; };
  }, [load]);
  return { session, reload: load, setSession };
}

/* Moves focus to an element once it has rendered (a notice or a heading). */
function useFocusOn<T extends HTMLElement>(): [RefObject<T | null>, () => void] {
  const ref = useRef<T | null>(null);
  const [request, setRequest] = useState(0);
  useEffect(() => {
    if (request) ref.current?.focus({ preventScroll: false });
  }, [request]);
  return [ref, useCallback(() => setRequest(n => n + 1), [])];
}

/* --- building blocks -------------------------------------------------------------------------- */

// The first account page rendered in this page load keeps the browser's own
// initial focus; later in-app navigation moves focus to the new page heading.
let firstAccountPage = true;

/* A short, route-specific assurance beside (or, on small screens, below) the
   task: only what this journey needs to know, stated as current fact. */
type Note = { title: string; body: string };

const NOTE_SIGN_IN: Note = {
  title: 'Private by default',
  body: 'Temporary chat remains available without an account. Signing in doesn’t attach the conversation you already have open.',
};
const NOTE_CREATE: Note = {
  title: 'Before you create one',
  body: 'Creating an account doesn’t attach the conversation you already have open, and temporary chat remains available. You decide what becomes part of your account.',
};
const NOTE_RECOVERY: Note = {
  title: 'About reset links',
  body: 'We never confirm whether an address has an account. Reset links work once and expire after 30 minutes.',
};
const NOTE_RECOVERY_CONFIRM: Note = {
  title: 'After the reset',
  body: 'Every device signed in to this account is signed out. Resetting doesn’t sign you in; you sign in with the new password.',
};
const NOTE_VERIFY: Note = {
  title: 'About verification',
  body: 'Verification confirms you can receive email at your account address. It doesn’t sign you in or change your password. Links expire after 24 hours.',
};
const NOTE_OVERVIEW: Note = {
  title: 'Your account and temporary chat',
  body: 'Your account currently holds your sign-in email, verification status, plan and billing settings. Temporary chat remains available, and you decide what becomes part of your account.',
};

function AccountLayout({ title, lede, children, note }:
    { title: string; lede?: ReactNode; children: ReactNode; note: Note }) {
  const heading = useRef<HTMLHeadingElement>(null);
  const noteId = useId();
  useEffect(() => {
    if (firstAccountPage) { firstAccountPage = false; return; }
    heading.current?.focus({ preventScroll: true });
  }, []);
  return (
    <main className="acct" id="main">
      <div className="acct-frame">
        <div className="acct-surface">
          <div className="acct-primary">
            <span className="eyebrow">Account</span>
            <h1 className="acct-title" id="acct-title" ref={heading} tabIndex={-1}>{title}</h1>
            {lede && <p className="acct-lede">{lede}</p>}
            <div className="acct-body">{children}</div>
          </div>
          <aside className="acct-aside" aria-labelledby={noteId}>
            <div className="acct-aside-note">
              <h2 id={noteId}>{note.title}</h2>
              <p>{note.body}</p>
            </div>
          </aside>
        </div>
      </div>
    </main>
  );
}

const Notice = ({ notice, focusRef }: { notice: NoticeData; focusRef?: RefObject<HTMLDivElement | null> }) => {
  const id = useId();
  return (
    <div className={`acct-notice acct-notice-${notice.tone}`} role="group" aria-labelledby={id}
         tabIndex={-1} ref={focusRef} data-testid="account-notice">
      {notice.tone === 'caution' && (
        <svg className="acct-notice-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2}
             strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" focusable="false">
          <circle cx="12" cy="12" r="9"/><path d="M12 7.5v5.5"/><path d="M12 16.5v.01"/>
        </svg>
      )}
      <div className="acct-notice-copy">
        <p className="acct-notice-title" id={id}>{notice.title}</p>
        {notice.body && <div className="acct-notice-body">{notice.body}</div>}
      </div>
    </div>
  );
};

type FieldProps = {
  id: string; label: string; kind: 'email' | 'password'; autoComplete: string;
  inputRef: RefObject<HTMLInputElement | null>; error?: string | null; hint?: string;
};

function Field({ id, label, kind, autoComplete, inputRef, error, hint }: FieldProps) {
  const [shown, setShown] = useState(false);
  const describedBy = [hint ? `${id}-hint` : '', `${id}-error`].filter(Boolean).join(' ');
  return (
    <div className="acct-field">
      <div className="acct-label-row">
        <label htmlFor={id}>{label}</label>
        {hint && <span className="acct-hint" id={`${id}-hint`}>{hint}</span>}
      </div>
      <div className={`acct-input${kind === 'password' ? ' acct-input-password' : ''}`}>
        <input
          id={id} ref={inputRef} name={id}
          type={kind === 'password' && !shown ? 'password' : kind === 'password' ? 'text' : 'email'}
          autoComplete={autoComplete} spellCheck={false} autoCapitalize="none" autoCorrect="off"
          inputMode={kind === 'email' ? 'email' : undefined}
          aria-invalid={error ? true : undefined} aria-describedby={describedBy}
        />
        {kind === 'password' && (
          <button type="button" className="acct-reveal" aria-controls={id} aria-pressed={shown}
                  onClick={() => setShown(!shown)}>
            {shown ? 'Hide' : 'Show'}<span className="sr-only"> password</span>
          </button>
        )}
      </div>
      <p className="acct-error" id={`${id}-error`}>{error ?? ''}</p>
    </div>
  );
}

/* Pending buttons stay focusable (no native disabled, so keyboard focus is
   not lost) but are marked aria-disabled; the pending guard ignores every
   activation until the request ends. */
function Spinner() {
  return <span className="acct-spinner" aria-hidden="true" />;
}

function SubmitButton({ pending, idle, busy }: { pending: boolean; idle: string; busy: string }) {
  return (
    <button type="submit" className="acct-button acct-button-primary" aria-disabled={pending || undefined}
            data-pending={pending || undefined}>
      {pending && <Spinner />}{pending ? busy : idle}
    </button>
  );
}

function Pending({ label }: { label: string }) {
  return <p className="acct-quiet" role="status"><Spinner />{label}</p>;
}

type UnavailableFeature = 'accounts' | 'recovery' | 'verification';

/* The feature is switched off on this server: the page says so in its heading
   and offers the one thing that works, instead of a form that can only fail. */
function UnavailablePage({ feature = 'accounts', note, retry }: { feature?: UnavailableFeature; note: Note; retry?: () => void }) {
  const title = feature === 'accounts' ? 'Accounts aren’t available here yet'
    : feature === 'recovery' ? 'Password reset isn’t available right now'
      : 'Email verification isn’t available right now';
  return (
    <AccountLayout title={title} note={note}>
      <div className="acct-state acct-state-unavailable" data-testid="account-unavailable" role={retry ? 'alert' : undefined}>
        <p>Kalillac works without an account. Temporary chat remains available.</p>
        <div className="acct-actions"><a className="acct-button acct-button-primary" href="/#chat">Start a private session</a></div>
        {retry && <div data-testid="registration-unavailable">
          <p>Registration availability could not be confirmed.</p>
          <button type="button" className="acct-button acct-button-secondary" onClick={retry}>Try again</button>
        </div>}
      </div>
    </AccountLayout>
  );
}

function Unreachable({ onRetry }: { onRetry: () => void }) {
  return (
    <div className="acct-state acct-state-problem" data-testid="account-unreachable">
      <p className="acct-state-title">Your account details couldn’t be loaded.</p>
      <p>Check your connection, then try again.</p>
      <div className="acct-actions">
        <button type="button" className="acct-button acct-button-secondary" onClick={onRetry}>Try again</button>
      </div>
    </div>
  );
}

function usePendingGuard() {
  const busy = useRef(false);
  const [pending, setPending] = useState(false);
  const run = useCallback(async (work: () => Promise<void>) => {
    if (busy.current) return;
    busy.current = true;
    setPending(true);
    try { await work(); } finally { busy.current = false; setPending(false); }
  }, []);
  return { pending, run };
}

function focusFirstInvalid(entries: [string | null | undefined, RefObject<HTMLInputElement | null>][]) {
  for (const [problem, ref] of entries) {
    if (problem) { ref.current?.focus(); return; }
  }
}

/* --- sign in ----------------------------------------------------------------------------------------- */

function SignInPage() {
  const [, navigate] = useLocation();
  const { session, reload, setSession } = useSession();
  const [notice, setNotice] = useState<NoticeData | null>(() => takeNotice());
  const [errors, setErrors] = useState<{ email?: string | null; password?: string | null }>({});
  const [noticeRef, focusNotice] = useFocusOn<HTMLDivElement>();
  const emailRef = useRef<HTMLInputElement>(null);
  const passwordRef = useRef<HTMLInputElement>(null);
  const { pending, run } = usePendingGuard();

  useEffect(() => {
    if (session.state === 'signed-in') navigate(ACCOUNT_PATHS.overview, { replace: true });
  }, [session, navigate]);
  useEffect(() => {
    if (session.state === 'signed-out' && emailRef.current && memory.email && !emailRef.current.value) {
      emailRef.current.value = memory.email;
    }
  }, [session]);

  const submit = (event: FormEvent) => {
    event.preventDefault();
    void run(async () => {
      const email = normalizedEmail(emailRef.current?.value ?? '');
      const password = passwordRef.current?.value ?? '';
      const problems = { email: emailProblem(email), password: password ? null : 'Enter your password.' };
      setErrors(problems);
      if (problems.email || problems.password) {
        focusFirstInvalid([[problems.email, emailRef], [problems.password, passwordRef]]);
        return;
      }
      const result = await api.login(email, password);
      if (result.ok) {
        if (passwordRef.current) passwordRef.current.value = '';
        memory.email = '';
        navigate(ACCOUNT_PATHS.overview);
        return;
      }
      if (result.status === 404) { setSession({ state: 'unavailable' }); return; }
      if (result.status === 422 && result.code === 'invalid_email') {
        setErrors({ email: 'Enter an email address like name@example.com.' });
        emailRef.current?.focus();
        return;
      }
      setNotice(result.status === 401 && result.code === 'invalid_credentials'
        ? { tone: 'caution', title: 'That email and password don’t match',
            body: 'Check both and try again. If you’ve forgotten your password, you can reset it.' }
        : result.status === 429
          ? { tone: 'error', title: 'Too many sign-in attempts', body: waitPhrase(result.retryAfter) }
          : UNREACHABLE);
      focusNotice();
    });
  };

  let body: ReactNode;
  if (session.state === 'loading' || session.state === 'signed-in') body = <Pending label="Checking your account…" />;
  else if (session.state === 'unavailable') return <UnavailablePage note={NOTE_SIGN_IN} />;
  else if (session.state === 'error') body = <Unreachable onRetry={() => void reload()} />;
  else body = (
    <>
      {notice && <Notice notice={notice} focusRef={noticeRef} />}
      <form className="acct-form" onSubmit={submit} noValidate aria-labelledby="acct-title">
        <Field id="email" label="Email" kind="email" autoComplete="email" inputRef={emailRef} error={errors.email} />
        <Field id="password" label="Password" kind="password" autoComplete="current-password"
               inputRef={passwordRef} error={errors.password} />
        <div className="acct-actions acct-actions-form">
          <SubmitButton pending={pending} idle="Sign in" busy="Signing in…" />
          <Link href={ACCOUNT_PATHS.recovery} className="acct-link" data-testid="link-forgot-password">Forgot password?</Link>
        </div>
      </form>
      <div className="acct-alternatives">
        <p>New to Kalillac? <Link href={ACCOUNT_PATHS.create} className="acct-link" data-testid="link-create-account">Create an account</Link></p>
        <p><a href="/#chat" className="acct-link acct-link-quiet" data-testid="link-continue-private">Continue without an account</a></p>
      </div>
    </>
  );

  return (
    <AccountLayout title="Sign in to Kalillac" note={NOTE_SIGN_IN}
                   lede="An account is optional. You can still use a private session without one.">
      {body}
    </AccountLayout>
  );
}

/* --- create account ------------------------------------------------------------------------------------- */

function CreateAccountPage({ verifyContract }: { verifyContract: () => Promise<boolean> }) {
  const [, navigate] = useLocation();
  const { session, reload, setSession } = useSession();
  const [created, setCreated] = useState(false);
  const [notice, setNotice] = useState<NoticeData | null>(null);
  const [errors, setErrors] = useState<{ email?: string | null; password?: string | null; confirm?: string | null }>({});
  const [noticeRef, focusNotice] = useFocusOn<HTMLDivElement>();
  const [doneRef, focusDone] = useFocusOn<HTMLDivElement>();
  const emailRef = useRef<HTMLInputElement>(null);
  const passwordRef = useRef<HTMLInputElement>(null);
  const confirmRef = useRef<HTMLInputElement>(null);
  const { pending, run } = usePendingGuard();

  useEffect(() => {
    if (session.state === 'signed-in') navigate(ACCOUNT_PATHS.overview, { replace: true });
  }, [session, navigate]);

  const submit = (event: FormEvent) => {
    event.preventDefault();
    void run(async () => {
      const email = normalizedEmail(emailRef.current?.value ?? '');
      const password = passwordRef.current?.value ?? '';
      const confirm = confirmRef.current?.value ?? '';
      const problems = {
        email: emailProblem(email),
        password: newPasswordProblem(password),
        confirm: !confirm ? 'Enter the password again.' : confirm !== password ? 'The passwords don’t match.' : null,
      };
      setErrors(problems);
      setNotice(null);
      if (problems.email || problems.password || problems.confirm) {
        focusFirstInvalid([[problems.email, emailRef], [problems.password, passwordRef], [problems.confirm, confirmRef]]);
        return;
      }
      if (!await verifyContract()) return;
      const result = await api.register(email, password);
      if (result.ok) {
        for (const ref of [passwordRef, confirmRef]) if (ref.current) ref.current.value = '';
        memory.email = email;
        setCreated(true);
        focusDone();
        return;
      }
      if (result.status === 404) { setSession({ state: 'unavailable' }); return; }
      if (result.status === 409 && result.code === 'email_already_registered') {
        setErrors({ email: 'An account already uses this email address.' });
        setNotice({ tone: 'error', title: 'This email address already has an account',
          body: <>You can <Link href={ACCOUNT_PATHS.signIn} className="acct-link">sign in</Link> or <Link href={ACCOUNT_PATHS.recovery} className="acct-link">reset the password</Link>.</> });
      } else if (result.status === 422 && result.code === 'invalid_email') {
        setErrors({ email: 'Enter an email address like name@example.com.' });
        emailRef.current?.focus();
        return;
      } else if (result.status === 422 && (result.code === 'password_length' || result.code === 'invalid_password')) {
        setErrors({ password: 'Use 12–256 characters.' });
        passwordRef.current?.focus();
        return;
      } else {
        setNotice(UNREACHABLE);
      }
      focusNotice();
    });
  };

  let body: ReactNode;
  if (session.state === 'loading' || session.state === 'signed-in') body = <Pending label="Checking your account…" />;
  else if (session.state === 'unavailable') return <UnavailablePage note={NOTE_CREATE} />;
  else if (session.state === 'error') body = <Unreachable onRetry={() => void reload()} />;
  else if (created) body = (
    <div className="acct-state acct-state-success" ref={doneRef} tabIndex={-1} data-testid="account-created">
      <p className="acct-state-title acct-state-done">Your account is ready.</p>
      <p>Creating an account doesn’t sign you in. Sign in with the email and password you just chose.</p>
      <div className="acct-actions">
        <Link href={ACCOUNT_PATHS.signIn} className="acct-button acct-button-primary">Continue to sign in</Link>
      </div>
    </div>
  );
  else body = (
    <>
      {notice && <Notice notice={notice} focusRef={noticeRef} />}
      <form className="acct-form" onSubmit={submit} noValidate aria-labelledby="acct-title">
        <Field id="email" label="Email" kind="email" autoComplete="email" inputRef={emailRef} error={errors.email} />
        <Field id="new-password" label="Password" kind="password" autoComplete="new-password"
               inputRef={passwordRef} error={errors.password} hint="Use 12–256 characters." />
        <Field id="confirm-password" label="Confirm password" kind="password" autoComplete="new-password"
               inputRef={confirmRef} error={errors.confirm} />
        <div className="acct-actions acct-actions-form">
          <SubmitButton pending={pending} idle="Create account" busy="Creating account…" />
        </div>
      </form>
      <div className="acct-alternatives">
        <p>Already have an account? <Link href={ACCOUNT_PATHS.signIn} className="acct-link">Sign in</Link></p>
      </div>
    </>
  );

  return (
    <AccountLayout title="Create an account" note={NOTE_CREATE}
                   lede="Your account keeps your plan and account settings connected across devices.">
      {body}
    </AccountLayout>
  );
}

/* --- password recovery --------------------------------------------------------------------------- */

function RecoveryRequestPage() {
  const { session, reload, setSession } = useSession();
  const [sent, setSent] = useState(false);
  const [notice, setNotice] = useState<NoticeData | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [noticeRef, focusNotice] = useFocusOn<HTMLDivElement>();
  const [doneRef, focusDone] = useFocusOn<HTMLDivElement>();
  const emailRef = useRef<HTMLInputElement>(null);
  const { pending, run } = usePendingGuard();

  const submit = (event: FormEvent) => {
    event.preventDefault();
    void run(async () => {
      const email = normalizedEmail(emailRef.current?.value ?? '');
      const problem = emailProblem(email);
      setError(problem);
      setNotice(null);
      if (problem) { emailRef.current?.focus(); return; }
      const result = await api.requestRecovery(email);
      if (result.ok) { setSent(true); focusDone(); return; }
      if (result.status === 404) { setSession({ state: 'unavailable' }); return; }
      if (result.status === 422 && result.code === 'invalid_email') {
        setError('Enter an email address like name@example.com.');
        emailRef.current?.focus();
        return;
      }
      setNotice(result.status === 429
        ? { tone: 'error', title: 'Too many reset requests', body: waitPhrase(result.retryAfter) }
        : UNREACHABLE);
      focusNotice();
    });
  };

  let body: ReactNode;
  if (session.state === 'loading') body = <Pending label="Checking your account…" />;
  else if (session.state === 'unavailable') return <UnavailablePage note={NOTE_RECOVERY} />;
  else if (session.state === 'error') body = <Unreachable onRetry={() => void reload()} />;
  else if (sent) body = (
    <div className="acct-state acct-state-info" ref={doneRef} tabIndex={-1} data-testid="recovery-requested">
      <p className="acct-state-title">Check your email.</p>
      <p>If an account can be reset for that address, a reset link will arrive there shortly. It works once and expires after 30 minutes.</p>
      <p>Nothing arrived? Check spam, or request another link in a few minutes.</p>
      <div className="acct-actions">
        <button type="button" className="acct-button acct-button-secondary" onClick={() => setSent(false)}>Request another link</button>
        <Link href={ACCOUNT_PATHS.signIn} className="acct-link">Back to sign in</Link>
      </div>
    </div>
  );
  else body = (
    <>
      {notice && <Notice notice={notice} focusRef={noticeRef} />}
      <form className="acct-form" onSubmit={submit} noValidate aria-labelledby="acct-title">
        <Field id="email" label="Email" kind="email" autoComplete="email" inputRef={emailRef} error={error} />
        <div className="acct-actions acct-actions-form">
          <SubmitButton pending={pending} idle="Send reset link" busy="Sending…" />
          <Link href={ACCOUNT_PATHS.signIn} className="acct-link">Back to sign in</Link>
        </div>
      </form>
    </>
  );

  return (
    <AccountLayout title="Reset your password" note={NOTE_RECOVERY}
                   lede="Enter the email address on your account. We’ll send a link to choose a new password.">
      {body}
    </AccountLayout>
  );
}

function InvalidLink({ kind }: { kind: 'recovery' | 'verification' }) {
  return (
    <div className="acct-state acct-state-problem" data-testid="invalid-link">
      <p className="acct-state-title">This link is invalid or has expired.</p>
      {kind === 'recovery' ? (
        <>
          <p>Reset links work once and expire after 30 minutes. Request a new one to continue.</p>
          <div className="acct-actions"><Link href={ACCOUNT_PATHS.recovery} className="acct-button acct-button-primary">Request a new link</Link></div>
        </>
      ) : (
        <>
          <p>Verification links work once and expire after 24 hours. Sign in to request a new one from your account.</p>
          <div className="acct-actions"><Link href={ACCOUNT_PATHS.overview} className="acct-button acct-button-primary">Go to your account</Link></div>
        </>
      )}
    </div>
  );
}

type RecoveryStage = 'form' | 'done' | 'invalid' | 'unavailable';

function RecoveryConfirmPage() {
  // Only proves the feature is available; a reset never depends on a session.
  const { session, reload } = useSession();
  const [notice, setNotice] = useState<NoticeData | null>(null);
  const [errors, setErrors] = useState<{ password?: string | null; confirm?: string | null }>({});
  const [noticeRef, focusNotice] = useFocusOn<HTMLDivElement>();
  const [stateRef, focusState] = useFocusOn<HTMLDivElement>();
  const passwordRef = useRef<HTMLInputElement>(null);
  const confirmRef = useRef<HTMLInputElement>(null);
  const { pending, run } = usePendingGuard();

  const { initial, credential, isCurrent } = useCredential(useCallback((next: Credential) => {
    for (const ref of [passwordRef, confirmRef]) if (ref.current) ref.current.value = '';
    setErrors({});
    setNotice(null);
    setStage(next.token ? 'form' : 'invalid');
  }, []));
  const [stage, setStage] = useState<RecoveryStage>(initial ? 'form' : 'invalid');

  const submit = (event: FormEvent) => {
    event.preventDefault();
    void run(async () => {
      const password = passwordRef.current?.value ?? '';
      const confirm = confirmRef.current?.value ?? '';
      const problems = {
        password: newPasswordProblem(password),
        confirm: !confirm ? 'Enter the password again.' : confirm !== password ? 'The passwords don’t match.' : null,
      };
      setErrors(problems);
      setNotice(null);
      if (problems.password || problems.confirm) {
        focusFirstInvalid([[problems.password, passwordRef], [problems.confirm, confirmRef]]);
        return;
      }
      const taken = credential.current;
      if (!taken.token) { setStage('invalid'); focusState(); return; }
      const result = await api.confirmRecovery(taken.token, password);
      // A newer link arrived while this request was in flight: its form,
      // fields and credential are not this response's to change.
      if (!isCurrent(taken)) return;
      if (result.ok || (result.status === 400 && result.code === 'invalid_or_expired_token')) {
        credential.current = { token: null, generation: taken.generation };
        for (const ref of [passwordRef, confirmRef]) if (ref.current) ref.current.value = '';
        setStage(result.ok ? 'done' : 'invalid');
        focusState();
        return;
      }
      if (result.status === 404) {
        credential.current = { token: null, generation: taken.generation };
        setStage('unavailable');
        return;
      }
      if (result.status === 422) {
        setErrors({ password: 'Use 12–256 characters.' });
        passwordRef.current?.focus();
        return;
      }
      setNotice(result.status === 429
        ? { tone: 'error', title: 'Too many attempts', body: waitPhrase(result.retryAfter) }
        : { tone: 'error', title: 'Your password wasn’t changed', body: 'Kalillac couldn’t complete the reset. Try again.' });
      focusNotice();
    });
  };

  let body: ReactNode;
  if (session.state === 'loading') body = <Pending label="Checking your link…" />;
  else if (session.state === 'unavailable' || stage === 'unavailable') {
    return <UnavailablePage feature={session.state === 'unavailable' ? 'accounts' : 'recovery'} note={NOTE_RECOVERY} />;
  }
  else if (session.state === 'error') body = <Unreachable onRetry={() => void reload()} />;
  else if (stage === 'invalid') body = <div ref={stateRef} tabIndex={-1}><InvalidLink kind="recovery" /></div>;
  else if (stage === 'done') body = (
    <div className="acct-state acct-state-success" ref={stateRef} tabIndex={-1} data-testid="recovery-complete">
      <p className="acct-state-title acct-state-done">Your password has been changed.</p>
      <p>Every device that was signed in to this account has been signed out. You haven’t been signed in here, so sign in with your new password to continue.</p>
      <div className="acct-actions">
        <Link href={ACCOUNT_PATHS.signIn} className="acct-button acct-button-primary">Sign in</Link>
      </div>
    </div>
  );
  else body = (
    <>
      {notice && <Notice notice={notice} focusRef={noticeRef} />}
      <form className="acct-form" onSubmit={submit} noValidate aria-labelledby="acct-title">
        {/* Browsers expect a username field in a password form and otherwise log
            a diagnostic stamped with the page address, which can briefly still
            hold a credential. This one is empty, hidden from people and
            assistive technology, never read and never sent. */}
        <input type="text" name="username" autoComplete="username" hidden readOnly tabIndex={-1}
               aria-hidden="true" data-testid="recovery-username-field" />
        <Field id="new-password" label="New password" kind="password" autoComplete="new-password"
               inputRef={passwordRef} error={errors.password} hint="Use 12–256 characters." />
        <Field id="confirm-password" label="Confirm new password" kind="password" autoComplete="new-password"
               inputRef={confirmRef} error={errors.confirm} />
        <div className="acct-actions acct-actions-form">
          <SubmitButton pending={pending} idle="Change password" busy="Changing password…" />
        </div>
      </form>
    </>
  );

  return (
    <AccountLayout title="Choose a new password" note={NOTE_RECOVERY_CONFIRM}
                   lede="Changing your password signs out every device that is signed in to this account.">
      {body}
    </AccountLayout>
  );
}

/* --- email verification ---------------------------------------------------------------------------------- */

type VerifyStage = 'confirming' | 'done' | 'invalid' | 'retry' | 'unavailable';

function VerifyEmailPage() {
  const { session, reload } = useSession();
  const [retryNotice, setRetryNotice] = useState<NoticeData | null>(null);
  const [stateRef, focusState] = useFocusOn<HTMLDivElement>();
  // The generation whose automatic confirmation has started.
  const started = useRef(0);
  // Changes with each later credential so the confirmation effect runs again.
  const [arrival, setArrival] = useState(1);

  const { initial, credential, isCurrent } = useCredential(useCallback((next: Credential) => {
    setRetryNotice(null);
    setStage(next.token ? 'confirming' : 'invalid');
    setArrival(next.generation);
  }, []));
  const [stage, setStage] = useState<VerifyStage>(initial ? 'confirming' : 'invalid');

  const confirm = useCallback(async () => {
    const taken = credential.current;
    if (!taken.token) { setStage('invalid'); return; }
    setStage('confirming');
    const result = await api.confirmVerification(taken.token);
    // Only the credential this page is still working with may change it.
    if (!isCurrent(taken)) return;
    if (result.ok || (result.status === 400 && result.code === 'invalid_or_expired_token')) {
      credential.current = { token: null, generation: taken.generation };
      setStage(result.ok ? 'done' : 'invalid');
    } else if (result.status === 404) {
      credential.current = { token: null, generation: taken.generation };
      setStage('unavailable');
    } else {
      setRetryNotice(result.status === 429
        ? { tone: 'error', title: 'Too many attempts', body: waitPhrase(result.retryAfter) }
        : { tone: 'error', title: 'Your email wasn’t verified yet', body: 'Kalillac couldn’t complete the check. Try again.' });
      setStage('retry');
    }
    focusState();
  }, [credential, isCurrent, focusState]);

  useEffect(() => {
    const ready = session.state === 'signed-in' || session.state === 'signed-out';
    const current = credential.current.generation;
    if (ready && stage === 'confirming' && started.current !== current) {
      started.current = current;
      void confirm();
    }
  }, [session, stage, confirm, arrival, credential]);

  let body: ReactNode;
  if (session.state === 'loading') body = <Pending label="Checking your link…" />;
  else if (session.state === 'unavailable') return <UnavailablePage note={NOTE_VERIFY} />;
  else if (stage === 'unavailable') return <UnavailablePage feature="verification" note={NOTE_VERIFY} />;
  else if (session.state === 'error') body = <Unreachable onRetry={() => void reload()} />;
  else if (stage === 'invalid') body = <div ref={stateRef} tabIndex={-1}><InvalidLink kind="verification" /></div>;
  else if (stage === 'confirming') body = <Pending label="Confirming your email address…" />;
  else if (stage === 'done') body = (
    <div className="acct-state acct-state-success" ref={stateRef} tabIndex={-1} data-testid="verification-complete">
      <p className="acct-state-title acct-state-done">Your email address is verified.</p>
      <p>This doesn’t sign you in or change your password.</p>
      <div className="acct-actions">
        <Link href={ACCOUNT_PATHS.overview} className="acct-button acct-button-secondary">Go to your account</Link>
      </div>
    </div>
  );
  else body = (
    <div ref={stateRef} tabIndex={-1}>
      {retryNotice && <Notice notice={retryNotice} />}
      <div className="acct-actions">
        <button type="button" className="acct-button acct-button-primary" onClick={() => void confirm()}>Try again</button>
      </div>
    </div>
  );

  return (
    <AccountLayout title="Verify your email" note={NOTE_VERIFY}>
      {body}
    </AccountLayout>
  );
}

/* --- account overview -------------------------------------------------------------------------------------- */

type Loadable<T> = { state: 'loading' } | { state: 'ready'; value: T } | { state: 'unavailable' } | { state: 'error' };

/* Stripe sends the customer back to the overview: Checkout's success and cancel URLs carry
   ?checkout=success or ?checkout=cancelled, the portal returns with no query (all three are server
   configuration). The value only chooses which fixed notice to show; nothing is trusted from it, and
   access always follows the plan the server reports. */
type CheckoutReturn = 'success' | 'cancelled' | null;

// Exactly the two configured queries and nothing else: the site parses no query (a source guard keeps
// it that way), so anything added to the address simply shows no notice.
function checkoutReturnFrom(search: string): CheckoutReturn {
  if (search === '?checkout=success') return 'success';
  if (search === '?checkout=cancelled') return 'cancelled';
  return null;
}

function checkoutReturnNotice(returned: CheckoutReturn, tier: api.Entitlements['tier']): NoticeData | null {
  if (returned === 'success') {
    return tier === 'paid'
      ? { tone: 'success', title: 'Your paid plan is active', body: 'Stripe has confirmed your payment.' }
      : { tone: 'info', title: 'Your payment is being confirmed',
          body: 'Paid access appears here once Stripe confirms the payment, usually within a few minutes.' };
  }
  if (returned === 'cancelled' && tier === 'free') {
    return { tone: 'info', title: 'Checkout was cancelled',
      body: 'You left Stripe’s checkout before paying, so your plan hasn’t changed.' };
  }
  return null;
}

function AccountOverviewPage() {
  const [, navigate] = useLocation();
  const { session, reload } = useSession();
  // Read once, without side effects; the query is then removed so a reload or a shared address
  // does not repeat the notice.
  const [checkoutReturn] = useState(() => checkoutReturnFrom(window.location.search));
  // From the chat: the Space and conversation to return to, and whether Billing was asked for.
  const [showBilling] = useState(() => takeArrival(window.location.search).billing);
  useEffect(() => {
    // Through the router (same path, so the page stays mounted); the only raw history change on these
    // pages remains the token-fragment scrub.
    if (window.location.search) navigate(ACCOUNT_PATHS.overview, { replace: true });
  }, [navigate]);
  const [plan, setPlan] = useState<Loadable<api.Entitlements>>({ state: 'loading' });
  const [billing, setBilling] = useState<Loadable<api.BillingStatus>>({ state: 'loading' });
  const [security, setSecurity] = useState<Loadable<api.AccountSecurity>>({ state: 'loading' });

  const sessionEnded = useCallback(() => {
    setPresence('signed-out');
    carryNotice(SESSION_ENDED);
    navigate(ACCOUNT_PATHS.signIn, { replace: true });
  }, [navigate]);

  const loadPlan = useCallback(async () => {
    setPlan({ state: 'loading' });
    const result = await api.getEntitlements();
    if (result.ok) setPlan({ state: 'ready', value: result.data });
    else if (result.status === 401) sessionEnded();
    else setPlan({ state: 'error' });
  }, [sessionEnded]);

  const loadBilling = useCallback(async () => {
    setBilling({ state: 'loading' });
    const result = await api.getBilling();
    if (result.ok) setBilling({ state: 'ready', value: result.data });
    else if (result.status === 404) setBilling({ state: 'unavailable' });
    else if (result.status === 401) sessionEnded();
    else setBilling({ state: 'error' });
  }, [sessionEnded]);

  // Offered only where the server provides them; anything but a clear answer shows none.
  const loadSecurity = useCallback(async () => {
    const result = await api.getAccountSecurity();
    if (result.ok) setSecurity({ state: 'ready', value: result.data });
    else if (result.status === 401) sessionEnded();
    else setSecurity({ state: 'unavailable' });
  }, [sessionEnded]);

  useEffect(() => {
    if (session.state === 'signed-out') navigate(ACCOUNT_PATHS.signIn, { replace: true });
    if (session.state === 'signed-in') { void loadPlan(); void loadBilling(); void loadSecurity(); }
  }, [session, navigate, loadPlan, loadBilling, loadSecurity]);

  let body: ReactNode;
  let lede: ReactNode;
  if (session.state === 'loading' || session.state === 'signed-out') body = <Pending label="Loading your account…" />;
  else if (session.state === 'unavailable') return <UnavailablePage note={NOTE_OVERVIEW} />;
  else if (session.state === 'error') body = <Unreachable onRetry={() => void reload()} />;
  else {
    lede = 'Your sign-in, verification, plan and billing settings, in one place.';
    body = <Overview user={session.user} plan={plan} billing={billing}
                     security={security.state === 'ready' ? security.value : null}
                     onRetryPlan={() => void loadPlan()}
                     onRetryBilling={() => void loadBilling()} onSessionEnded={sessionEnded}
                     checkoutReturn={checkoutReturn} showBilling={showBilling}
                     onRefreshPlan={() => { void loadPlan(); void loadBilling(); }} />;
  }

  return (
    <AccountLayout title="Your account" lede={lede} note={NOTE_OVERVIEW}>
      {body}
    </AccountLayout>
  );
}

function Overview({ user, plan, billing, security, onRetryPlan, onRetryBilling, onSessionEnded, checkoutReturn,
                   showBilling = false, onRefreshPlan }: {
  user: api.AccountUser; plan: Loadable<api.Entitlements>; billing: Loadable<api.BillingStatus>;
  security: api.AccountSecurity | null;
  onRetryPlan: () => void; onRetryBilling: () => void; onSessionEnded: () => void;
  checkoutReturn: CheckoutReturn; showBilling?: boolean; onRefreshPlan: () => void;
}) {
  const [, navigate] = useLocation();
  const { returnTarget } = useCustomerSession();
  // Asked for Billing (the header's Billing, or the chat's): brought into view once it has loaded.
  const billingShown = useRef(!showBilling);
  const billingReady = billing.state !== 'loading' && plan.state !== 'loading';
  useEffect(() => {
    if (billingShown.current || !billingReady) return;
    billingShown.current = true;
    const heading = document.getElementById('acct-billing-heading');
    if (heading) heading.scrollIntoView({ block: 'start' });
  }, [billingReady]);
  const memberSince = formatDate(user.created_at, false);
  const [verification, setVerification] = useState<NoticeData | null>(null);
  const [verificationRef, focusVerification] = useFocusOn<HTMLDivElement>();
  const [signOutNotice, setSignOutNotice] = useState<NoticeData | null>(null);
  const [signOutRef, focusSignOut] = useFocusOn<HTMLDivElement>();
  const verify = usePendingGuard();
  const signOut = usePendingGuard();
  const tier = plan.state === 'ready' ? plan.value.tier : null;

  const requestVerification = () => void verify.run(async () => {
    const result = await api.requestVerification();
    if (result.ok) {
      setVerification({ tone: 'success', title: 'Verification email requested',
        body: 'If it can be delivered, a link will arrive at your account address shortly. It expires after 24 hours.' });
    } else if (result.status === 401) { onSessionEnded(); return; }
    else if (result.status === 429) setVerification({ tone: 'error', title: 'Too many requests', body: waitPhrase(result.retryAfter) });
    else if (result.status === 404) setVerification({ tone: 'info', title: 'Email verification isn’t available right now' });
    else setVerification(UNREACHABLE);
    focusVerification();
  });

  const doSignOut = () => void signOut.run(async () => {
    const result = await api.logout();
    if (result.ok) {
      setPresence('signed-out');
      carryNotice(SIGNED_OUT);
      navigate(ACCOUNT_PATHS.signIn);
      return;
    }
    setSignOutNotice(UNREACHABLE);
    focusSignOut();
  });

  return (
    <div className="acct-overview">
      {returnTarget && (
        <a className="acct-return" href={chatHref(returnTarget)} data-testid="link-return-to-space">
          <span aria-hidden="true">←</span>
          {returnTarget.conversation ? 'Back to your Space and conversation' : 'Back to your Space'}
        </a>
      )}
      <section className="acct-identity" aria-labelledby="acct-details-heading" data-tier={tier ?? undefined}>
        <h2 id="acct-details-heading" className="sr-only">Account details</h2>
        <div className="acct-identity-main">
          <p className="acct-identity-email" data-testid="account-email">{user.email}</p>
          {memberSince && <p className="acct-identity-meta">Member since <span data-testid="account-member-since">{memberSince}</span></p>}
        </div>
        <p className="acct-plan" data-testid="account-plan">
          {plan.state === 'ready' ? (plan.value.tier === 'paid'
              ? <><svg className="acct-premium-mark" viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path d="M12 2.5l2.6 6.2 6.4.5-4.9 4.2 1.5 6.3L12 16.4l-5.6 3.3 1.5-6.3L3 9.2l6.4-.5z" fill="currentColor"/></svg><span>Kalillac Premium</span></> : 'Free account')
            : plan.state === 'loading' ? <span className="acct-quiet-inline">Loading…</span>
              : <span className="acct-inline-problem">Couldn’t load your plan. <button type="button" className="acct-text-button" onClick={onRetryPlan}>Try again</button></span>}
        </p>
      </section>

      {user.email_verified !== undefined && (
        <section className="acct-section" aria-labelledby="acct-verification-heading">
          <div className="acct-section-head">
            <h2 id="acct-verification-heading" className="acct-section-title">Email verification</h2>
            <div className="acct-section-value" data-testid="account-verification">
              {user.email_verified ? <span className="acct-verified">Verified</span> : (
                <div className="acct-row-detail">
                  <span className="acct-unverified">Not verified yet</span>
                  <button type="button" className="acct-button acct-button-secondary acct-button-compact"
                          aria-disabled={verify.pending || undefined} onClick={requestVerification}
                          data-testid="button-request-verification">
                    {verify.pending && <Spinner />}{verify.pending ? 'Requesting…' : 'Send verification email'}
                  </button>
                </div>
              )}
            </div>
          </div>
          {verification && <Notice notice={verification} focusRef={verificationRef} />}
        </section>
      )}

      {tier === 'free' && billing.state === 'ready' && <PremiumDiscovery />}

      {billing.state === 'error' && (
        <section className="acct-section" aria-labelledby="acct-billing-heading">
          <h2 id="acct-billing-heading" className="acct-section-title">Plan and billing</h2>
          <p className="acct-inline-problem">Billing details couldn’t be loaded. <button type="button" className="acct-text-button" onClick={onRetryBilling}>Try again</button></p>
        </section>
      )}
      {billing.state === 'ready' && plan.state === 'ready' && (
        <BillingSection status={billing.value} tier={plan.value.tier} emailVerified={user.email_verified}
                        onSessionEnded={onSessionEnded} checkoutReturn={checkoutReturn} onRefreshPlan={onRefreshPlan} />
      )}

      <section className="acct-section" aria-labelledby="acct-session-heading">
        <h2 id="acct-session-heading" className="acct-section-title">Security</h2>
        <p className="acct-section-text">Signing out ends this browser’s account session. Temporary chat isn’t affected.</p>
        {signOutNotice && <Notice notice={signOutNotice} focusRef={signOutRef} />}
        <div className="acct-actions">
          <button type="button" className="acct-button acct-button-secondary" aria-disabled={signOut.pending || undefined}
                  onClick={doSignOut} data-testid="button-sign-out">
            {signOut.pending && <Spinner />}{signOut.pending ? 'Signing out…' : 'Sign out'}
          </button>
        </div>
      </section>

      {FILES_OFFERED && <FilesSection onSessionEnded={onSessionEnded} />}
      {security?.sessions && <SessionsSection onSessionEnded={onSessionEnded} />}
      {security?.password_change && <PasswordSection onSessionEnded={onSessionEnded} />}
      {security?.deletion_request && <DeletionSection paid={tier === 'paid'} onSessionEnded={onSessionEnded} />}
    </div>
  );
}

/* --- Kalillac Premium, for a Free account ------------------------------------------------------------------------
   What Premium adds here, as practical benefits, and nothing this deployment does not offer (account-api
   getPremiumCapabilities). No amount is stated (this page is not told one) and there is no urgency: Explore
   Kalillac Premium only brings Plan and billing, just below, into view, where the upgrade and Stripe's checkout
   (where the cost is shown before any payment) are. */

type PremiumItem = { key: string; title: string; text: string; icon: ReactNode };

const PREMIUM_ICON_PROPS = {
  viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor', strokeWidth: 1.8, strokeLinecap: 'round' as const,
  strokeLinejoin: 'round' as const, 'aria-hidden': true, focusable: false,
};

function PremiumDiscovery() {
  const [capabilities, setCapabilities] = useState<api.PremiumCapabilities | null>(null);
  useEffect(() => {
    let alive = true;
    void api.getPremiumCapabilities(FILES_OFFERED).then((found) => { if (alive) setCapabilities(found); });
    return () => { alive = false; };
  }, []);
  if (!capabilities) return null;
  const items: PremiumItem[] = [];
  if (capabilities.deepReasoning) {
    items.push({ key: 'deep', title: 'Deep reasoning',
      text: 'Extra reasoning effort for demanding work: complex programming problems, mathematical reasoning and multi-step analysis. Choose Deep in the chat when a question calls for it.',
      icon: <svg {...PREMIUM_ICON_PROPS}><path d="M12 3l8 4-8 4-8-4z"/><path d="M4 12l8 4 8-4"/><path d="M4 17l8 4 8-4"/></svg> });
  }
  if (capabilities.files) {
    items.push({ key: 'files', title: 'Files',
      text: 'Attach supported documents to a message and ask Kalillac to summarize or work with them, and have it create Word, Excel, PowerPoint and PDF files you can download.',
      icon: <svg {...PREMIUM_ICON_PROPS}><path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5"/><path d="M9 14h6"/><path d="M9 17h4"/></svg> });
  }
  if (!items.length) return null;
  // Plan and billing, brought into view and focused (its heading), as the header's Billing link does.
  const explore = () => {
    const heading = document.getElementById('acct-billing-heading');
    if (!heading) return;
    heading.scrollIntoView({ block: 'start' });
    heading.focus({ preventScroll: true });
  };
  return (
    <section className="acct-section acct-premium" aria-labelledby="acct-premium-heading" data-testid="premium-discovery">
      <h2 id="acct-premium-heading" className="acct-section-title acct-premium-title">
        <svg className="acct-premium-mark" viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path d="M12 2.5l2.6 6.2 6.4.5-4.9 4.2 1.5 6.3L12 16.4l-5.6 3.3 1.5-6.3L3 9.2l6.4-.5z" fill="currentColor"/></svg><span>What Kalillac Premium adds</span>
      </h2>
      <ul className="acct-premium-list">
        {items.map((item) => (
          <li key={item.key} data-testid={`premium-${item.key}`}>
            <span className="acct-premium-icon">{item.icon}</span>
            <span><strong>{item.title}</strong><span className="acct-premium-text">{item.text}</span></span>
          </li>
        ))}
      </ul>
      <div className="acct-premium-more">
        <button type="button" className="acct-button acct-button-secondary acct-button-compact" onClick={explore}
                data-testid="button-explore-premium">
          Explore Kalillac Premium
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round"
               strokeLinejoin="round" aria-hidden="true" focusable="false"><path d="M12 5v14"/><path d="m6 13 6 6 6-6"/></svg>
        </button>
      </div>
    </section>
  );
}

/* --- the account's stored files (a files deployment: data-kalillac-files="on") ----------------------------------
   Every file the account holds, expired ones included: a customer can download a file while it is available and
   delete any of them, whatever the plan (Codex F6). Nothing here uploads or makes a file. */

const FILES_OFFERED = document.documentElement.getAttribute('data-kalillac-files') === 'on';

function fileSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} ${bytes === 1 ? 'byte' : 'bytes'}`;
  const kib = bytes / 1024;
  if (kib < 1024) return `${kib < 10 ? kib.toFixed(1) : Math.round(kib)} KB`;
  const mib = kib / 1024;
  return `${mib < 10 ? mib.toFixed(1) : Math.round(mib)} MB`;
}

function FilesSection({ onSessionEnded }: { onSessionEnded: () => void }) {
  const [files, setFiles] = useState<Loadable<api.StoredFile[]>>({ state: 'loading' });
  const [confirming, setConfirming] = useState<string | null>(null);
  const [notice, setNotice] = useState<NoticeData | null>(null);
  const [noticeRef, focusNotice] = useFocusOn<HTMLDivElement>();
  const remove = usePendingGuard();

  const load = useCallback(async () => {
    setFiles({ state: 'loading' });
    const result = await api.listFiles();
    if (result.ok) setFiles({ state: 'ready', value: result.data });
    else if (result.status === 401) onSessionEnded();
    else setFiles({ state: 'error' });
  }, [onSessionEnded]);
  useEffect(() => { void load(); }, [load]);

  const deleteFile = (file: api.StoredFile) => void remove.run(async () => {
    setNotice(null);
    const result = await api.deleteFile(file.id);
    if (result.ok || result.status === 404) {   // Gone either way: it is no longer in the account.
      setConfirming(null);
      setFiles((current) => current.state === 'ready'
        ? { state: 'ready', value: current.value.filter((item) => item.id !== file.id) } : current);
      setNotice({ tone: 'success', title: 'File deleted', body: `${file.filename} was removed from your account.` });
    } else if (result.status === 401) { onSessionEnded(); return; }
    else setNotice(result.status === 0 ? UNREACHABLE : SECURITY_UNAVAILABLE);
    focusNotice();
  });

  return (
    <section className="acct-section" aria-labelledby="acct-files-heading" data-testid="files-section">
      <h2 id="acct-files-heading" className="acct-section-title">Your files</h2>
      <p className="acct-section-text">
        Files you attach or Kalillac creates are stored in your account and stay available for 30 days, even when
        the conversation was temporary. You can download a file while it’s available and delete any file at any time.
      </p>
      {notice && <Notice notice={notice} focusRef={noticeRef} />}
      {files.state === 'loading' && <p className="acct-quiet-inline">Loading your files…</p>}
      {files.state === 'error' && (
        <p className="acct-inline-problem">Your files couldn’t be loaded. <button type="button" className="acct-text-button"
          onClick={() => void load()}>Try again</button></p>
      )}
      {files.state === 'ready' && files.value.length === 0 && (
        <p className="acct-section-text" data-testid="files-empty">No files are stored in your account.</p>
      )}
      {files.state === 'ready' && files.value.length > 0 && (
        <ul className="acct-files" data-testid="files-list">
          {files.value.map((file) => {
            const until = formatDate(file.expires_at, true);
            const download = file.available ? api.fileDownloadPath(file.id) : null;
            return (
              <li key={file.id} className="acct-file" data-testid="file-row" data-available={file.available}>
                <div className="acct-file-main">
                  <span className="acct-file-name">{file.filename}</span>
                  <span className="acct-file-meta">
                    {fileSize(file.size)} · {file.available
                      ? (until ? `Available until ${until}` : 'Available')
                      : 'Expired, no longer downloadable'}
                  </span>
                </div>
                {confirming === file.id ? (
                  <div className="acct-file-actions" role="group" aria-label={`Delete ${file.filename}?`}>
                    <span className="acct-file-confirm">Delete it permanently?</span>
                    <button type="button" className="acct-button acct-button-danger acct-button-compact"
                            aria-disabled={remove.pending || undefined} onClick={() => deleteFile(file)}
                            data-testid="button-confirm-delete-file">
                      {remove.pending && <Spinner />}{remove.pending ? 'Deleting…' : 'Delete'}
                    </button>
                    <button type="button" className="acct-button acct-button-secondary acct-button-compact"
                            onClick={() => setConfirming(null)} data-testid="button-cancel-delete-file">Cancel</button>
                  </div>
                ) : (
                  <div className="acct-file-actions">
                    {download && <a className="acct-button acct-button-secondary acct-button-compact" href={download}
                                    download={file.filename} data-testid="link-download-file">Download</a>}
                    <button type="button" className="acct-button acct-button-secondary acct-button-compact"
                            onClick={() => { setNotice(null); setConfirming(file.id); }}
                            aria-label={`Delete ${file.filename}`} data-testid="button-delete-file">Delete</button>
                  </div>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}

/* --- account security: sessions, password, deletion request --------------------------------------------------- */

const SECURITY_UNAVAILABLE: NoticeData = {
  tone: 'error', title: 'That couldn’t be completed right now', body: 'Try again in a few minutes.',
};

/* Every failure these operations share. A wrong current password is handled by its own field. */
function securityProblem(result: api.ApiError): NoticeData {
  if (result.status === 0) return UNREACHABLE;
  if (result.status === 429) return { tone: 'error', title: 'Too many attempts', body: waitPhrase(result.retryAfter) };
  if (result.status === 403 && result.code === 'account_unavailable') {
    return { tone: 'error', title: 'This account can’t be changed right now' };
  }
  return SECURITY_UNAVAILABLE;
}

function SessionsSection({ onSessionEnded }: { onSessionEnded: () => void }) {
  const [, navigate] = useLocation();
  const [notice, setNotice] = useState<NoticeData | null>(null);
  const [noticeRef, focusNotice] = useFocusOn<HTMLDivElement>();
  const others = usePendingGuard();
  const everywhere = usePendingGuard();

  const signOutOthers = () => void others.run(async () => {
    setNotice(null);
    const result = await api.revokeOtherSessions();
    if (result.ok) {
      setNotice({ tone: 'success', title: 'Other sessions were signed out', body: 'This browser stays signed in.' });
    } else if (result.status === 401) { onSessionEnded(); return; }
    else setNotice(securityProblem(result));
    focusNotice();
  });

  const signOutEverywhere = () => void everywhere.run(async () => {
    setNotice(null);
    const result = await api.revokeAllSessions();
    if (result.ok) {
      carryNotice({ tone: 'info', title: 'You’re signed out everywhere', body: 'Sign in again to continue.' });
      navigate(ACCOUNT_PATHS.signIn);
      return;
    }
    if (result.status === 401) { onSessionEnded(); return; }
    setNotice(securityProblem(result));
    focusNotice();
  });

  return (
    <section className="acct-section" aria-labelledby="acct-sessions-heading" data-testid="sessions-section">
      <h2 id="acct-sessions-heading" className="acct-section-title">Signed-in sessions</h2>
      <p className="acct-section-text">If you signed in on a device you no longer use, sign that session out here.</p>
      {notice && <Notice notice={notice} focusRef={noticeRef} />}
      <div className="acct-actions">
        <button type="button" className="acct-button acct-button-secondary" aria-disabled={others.pending || undefined}
                onClick={signOutOthers} data-testid="button-revoke-others">
          {others.pending && <Spinner />}{others.pending ? 'Signing out…' : 'Sign out other sessions'}
        </button>
        <button type="button" className="acct-button acct-button-secondary" aria-disabled={everywhere.pending || undefined}
                onClick={signOutEverywhere} data-testid="button-revoke-all">
          {everywhere.pending && <Spinner />}{everywhere.pending ? 'Signing out…' : 'Sign out everywhere'}
        </button>
      </div>
    </section>
  );
}

function PasswordSection({ onSessionEnded }: { onSessionEnded: () => void }) {
  const current = useRef<HTMLInputElement>(null);
  const next = useRef<HTMLInputElement>(null);
  const [currentError, setCurrentError] = useState<string | null>(null);
  const [nextError, setNextError] = useState<string | null>(null);
  const [notice, setNotice] = useState<NoticeData | null>(null);
  const [noticeRef, focusNotice] = useFocusOn<HTMLDivElement>();
  const guard = usePendingGuard();

  const submit = (event: FormEvent) => {
    event.preventDefault();
    void guard.run(async () => {
      const currentValue = current.current?.value ?? '';
      const nextValue = next.current?.value ?? '';
      const currentProblem = currentValue ? null : 'Enter your current password.';
      const nextProblem = newPasswordProblem(nextValue);
      setCurrentError(currentProblem);
      setNextError(nextProblem);
      setNotice(null);
      if (currentProblem || nextProblem) {
        focusFirstInvalid([[currentProblem, current], [nextProblem, next]]);
        return;
      }
      const result = await api.changePassword(currentValue, nextValue);
      if (result.ok) {
        if (current.current) current.current.value = '';
        if (next.current) next.current.value = '';
        setNotice({ tone: 'success', title: 'Your password was changed',
          body: 'Every other session of this account was signed out. This browser stays signed in.' });
        focusNotice();
        return;
      }
      if (result.status === 401) { onSessionEnded(); return; }
      if (result.status === 403 && result.code === 'invalid_current_password') {
        setCurrentError('That isn’t your current password.');
        current.current?.focus();
        return;
      }
      if (result.status === 422 && result.code === 'password_length') {
        setNextError('Use 12–256 characters.');
        next.current?.focus();
        return;
      }
      setNotice(securityProblem(result));
      focusNotice();
    });
  };

  return (
    <section className="acct-section" aria-labelledby="acct-password-heading" data-testid="password-section">
      <h2 id="acct-password-heading" className="acct-section-title">Password</h2>
      <p className="acct-section-text">Changing your password signs out every other session of this account.</p>
      {notice && <Notice notice={notice} focusRef={noticeRef} />}
      <form className="acct-form acct-form-inline" onSubmit={submit} noValidate aria-labelledby="acct-password-heading">
        <Field id="security-current-password" label="Current password" kind="password" autoComplete="current-password"
               inputRef={current} error={currentError} />
        <Field id="security-new-password" label="New password" kind="password" autoComplete="new-password"
               inputRef={next} error={nextError} hint="12–256 characters" />
        <div className="acct-actions acct-actions-form">
          <SubmitButton pending={guard.pending} idle="Change password" busy="Changing…" />
        </div>
      </form>
    </section>
  );
}

function DeletionSection({ paid, onSessionEnded }: { paid: boolean; onSessionEnded: () => void }) {
  const [, navigate] = useLocation();
  const [open, setOpen] = useState(false);
  const password = useRef<HTMLInputElement>(null);
  const [passwordError, setPasswordError] = useState<string | null>(null);
  const [notice, setNotice] = useState<NoticeData | null>(null);
  const [noticeRef, focusNotice] = useFocusOn<HTMLDivElement>();
  const guard = usePendingGuard();

  useEffect(() => {
    if (open) password.current?.focus();
  }, [open]);

  const submit = (event: FormEvent) => {
    event.preventDefault();
    void guard.run(async () => {
      const value = password.current?.value ?? '';
      const problem = value ? null : 'Enter your current password.';
      setPasswordError(problem);
      setNotice(null);
      if (problem) { password.current?.focus(); return; }
      const result = await api.requestAccountDeletion(value);
      if (result.ok) {
        carryNotice({ tone: 'info', title: 'Your deletion request was received',
          body: 'This account is signed out everywhere and can no longer be used to sign in.' });
        navigate(ACCOUNT_PATHS.signIn);
        return;
      }
      if (result.status === 401) { onSessionEnded(); return; }
      if (result.status === 403 && result.code === 'invalid_current_password') {
        setPasswordError('That isn’t your current password.');
        password.current?.focus();
        return;
      }
      setNotice(securityProblem(result));
      focusNotice();
    });
  };

  return (
    <section className="acct-section" aria-labelledby="acct-deletion-heading" data-testid="deletion-section">
      <h2 id="acct-deletion-heading" className="acct-section-title">Delete this account</h2>
      <p className="acct-section-text">
        Requesting deletion signs this account out everywhere and closes sign-in to it. It can’t be undone from this page.
      </p>
      {paid && (
        <p className="acct-section-text" data-testid="deletion-subscription-note">
          This request doesn’t cancel a paid subscription. Cancel it first in Manage billing.
        </p>
      )}
      {notice && <Notice notice={notice} focusRef={noticeRef} />}
      {!open ? (
        <div className="acct-actions">
          <button type="button" className="acct-button acct-button-secondary" onClick={() => setOpen(true)}
                  aria-expanded={false} data-testid="button-open-deletion">
            Request account deletion
          </button>
        </div>
      ) : (
        <form className="acct-form acct-form-inline" onSubmit={submit} noValidate aria-labelledby="acct-deletion-heading">
          <Field id="deletion-current-password" label="Current password, to confirm" kind="password"
                 autoComplete="current-password" inputRef={password} error={passwordError} />
          <div className="acct-actions acct-actions-form">
            <button type="submit" className="acct-button acct-button-danger" aria-disabled={guard.pending || undefined}
                    data-pending={guard.pending || undefined} data-testid="button-request-deletion">
              {guard.pending && <Spinner />}{guard.pending ? 'Requesting…' : 'Request deletion of this account'}
            </button>
            <button type="button" className="acct-text-button" onClick={() => { setOpen(false); setNotice(null); }}>
              Keep my account
            </button>
          </div>
        </form>
      )}
    </section>
  );
}


const CHECKOUT_MESSAGES: Record<string, NoticeData> = {
  email_verification_required: { tone: 'info', title: 'Verify your email first',
    body: 'Verify your account’s email address before starting a paid plan. You can send a verification email above.' },
  already_subscribed: { tone: 'info', title: 'This account already has a subscription',
    body: 'Use Manage billing to change it.' },
  subscription_unresolved: { tone: 'error', title: 'A previous subscription needs attention',
    body: 'Resolve it in Manage billing before starting a new one.' },
  billing_processing: { tone: 'info', title: 'Your payment is being confirmed',
    body: 'Paid access appears here once Stripe confirms the payment. Check back in a few minutes.' },
  billing_profile_unavailable: { tone: 'info', title: 'There’s no billing profile for this account yet',
    body: 'A billing profile is created when you start a paid plan.' },
  billing_unavailable: { tone: 'error', title: 'Billing is temporarily unavailable',
    body: 'Try again in a few minutes.' },
  untrusted_redirect: { tone: 'error', title: 'The billing page couldn’t be opened',
    body: 'Try again in a few minutes.' },
};

function BillingSection({ status, tier, emailVerified, onSessionEnded, checkoutReturn = null, onRefreshPlan }: {
  status: api.BillingStatus; tier: api.Entitlements['tier']; emailVerified: boolean | undefined; onSessionEnded: () => void;
  checkoutReturn?: CheckoutReturn; onRefreshPlan?: () => void;
}) {
  const [notice, setNotice] = useState<NoticeData | null>(null);
  const returned = checkoutReturnNotice(checkoutReturn, tier);
  const awaitingConfirmation = checkoutReturn === 'success' && tier === 'free';
  const [noticeRef, focusNotice] = useFocusOn<HTMLDivElement>();
  const checkout = usePendingGuard();
  const portal = usePendingGuard();
  const periodEnd = formatDate(status.current_period_end, true);
  const needsVerification = tier === 'free' && emailVerified === false;
  const paymentProblem = status.subscription_status === 'past_due' || status.subscription_status === 'unpaid';

  const go = (guard: ReturnType<typeof usePendingGuard>, start: () => Promise<api.ApiResult<string>>) =>
    () => void guard.run(async () => {
      setNotice(null);
      const result = await start();
      if (result.ok) { window.location.assign(result.data); return; }
      if (result.status === 401) { onSessionEnded(); return; }
      setNotice((result.code && CHECKOUT_MESSAGES[result.code]) || UNREACHABLE);
      focusNotice();
    });

  return (
    <section className="acct-section" aria-labelledby="acct-billing-heading" data-testid="billing-section">
      <h2 id="acct-billing-heading" className="acct-section-title" tabIndex={-1}>Plan and billing</h2>
      {returned && !notice && (
        <div data-testid="checkout-return">
          <Notice notice={returned} />
          {awaitingConfirmation && onRefreshPlan && (
            <div className="acct-actions acct-actions-return">
              <button type="button" className="acct-button acct-button-secondary acct-button-compact"
                      onClick={onRefreshPlan} data-testid="button-check-plan">Check my plan again</button>
            </div>
          )}
        </div>
      )}
      {tier === 'paid' && periodEnd && (
        <p className="acct-section-text" data-testid="billing-period">
          {status.cancel_at_period_end
            ? <>Your plan is set to end. The current billing period ends on {periodEnd}, and it won’t renew.</>
            : <>The current billing period ends on {periodEnd}.</>}
        </p>
      )}
      {paymentProblem && status.portal_available && (
        <p className="acct-section-text" data-testid="billing-payment-problem">Stripe couldn’t collect the last payment. Update your payment method in Manage billing.</p>
      )}
      {tier === 'free' && !awaitingConfirmation && (needsVerification
        ? <p className="acct-section-text" data-testid="billing-needs-verification">Verify your email address before starting a paid plan.</p>
        : <p className="acct-section-text" data-testid="billing-upgrade-note">Upgrading opens Stripe’s secure checkout, where you can review what Kalillac Premium costs before you pay. Paid access is added after Stripe confirms the payment, which can take a few minutes.</p>)}
      {notice && <Notice notice={notice} focusRef={noticeRef} />}
      {/* Just back from a paid Checkout: no second purchase is offered while Stripe confirms the first. */}
      {(status.portal_available || (tier === 'free' && !needsVerification && !awaitingConfirmation)) && (
        <div className="acct-actions">
          {tier === 'free' && !needsVerification && !awaitingConfirmation && (
            <button type="button" className="acct-button acct-button-primary" aria-disabled={checkout.pending || undefined}
                    onClick={go(checkout, api.startCheckout)} data-testid="button-upgrade">
              {checkout.pending && <Spinner />}{checkout.pending ? 'Opening checkout…' : 'Upgrade to Kalillac Premium'}
            </button>
          )}
          {status.portal_available && (
            <button type="button" className="acct-button acct-button-secondary" aria-disabled={portal.pending || undefined}
                    onClick={go(portal, api.openBillingPortal)} data-testid="button-manage-billing">
              {portal.pending && <Spinner />}{portal.pending ? 'Opening billing…' : 'Manage billing'}
            </button>
          )}
        </div>
      )}
    </section>
  );
}

/* --- routes ------------------------------------------------------------------------------------------------- */

export default function AccountPages() {
  const [location] = useLocation();
  switch (location) {
    case ACCOUNT_PATHS.signIn: return <SignInPage />;
    case ACCOUNT_PATHS.create: return <AuthoritativeRegistrationPage />;
    case ACCOUNT_PATHS.createConfirm: return <RegistrationConfirmPage />;
    case ACCOUNT_PATHS.overview: return <AccountOverviewPage />;
    case ACCOUNT_PATHS.recovery: return <RecoveryRequestPage />;
    case ACCOUNT_PATHS.recoveryConfirm: return <RecoveryConfirmPage />;
    case ACCOUNT_PATHS.verify: return <VerifyEmailPage />;
    // Earlier staging documents named /account/billing as Stripe's return address; it is the overview.
    case ACCOUNT_PATHS.overview + '/billing':
      return <Redirect to={ACCOUNT_PATHS.overview + window.location.search} replace />;
    default: return <NotFound />;
  }
}

/* Approved default-off email-first flow, using the existing account components. */
function CreateEmailFirstPage({ verifyContract }: { verifyContract: () => Promise<boolean> }) {
  const [sent, setSent] = useState(false);
  const [unavailable, setUnavailable] = useState(false);
  const [notice, setNotice] = useState<NoticeData | null>(null);
  const [error, setError] = useState<string | null>(null);
  const emailRef = useRef<HTMLInputElement>(null);
  const submitted = useRef('');
  const alive = useRef(true);
  const [noticeRef, focusNotice] = useFocusOn<HTMLDivElement>();
  const [doneRef, focusDone] = useFocusOn<HTMLDivElement>();
  const { pending, run } = usePendingGuard();
  // Signed in already (asked here, on a fresh load too): the account, not registration.
  const { session } = useSession();
  const [, navigate] = useLocation();
  useEffect(() => {
    if (session.state === 'signed-in') navigate(ACCOUNT_PATHS.overview, { replace: true });
  }, [session, navigate]);
  const send = (resend: boolean) => {
    void run(async () => {
      const email = resend ? submitted.current : normalizedEmail(emailRef.current?.value ?? '');
      const problem = !email ? 'Enter your email address.' : Array.from(email).length > EMAIL_MAX || !V2_EMAIL_SHAPE.test(email) ? 'Enter an email address like name@example.com.' : null;
      setError(problem); setNotice(null);
      if (problem) { emailRef.current?.focus(); return; }
      if (!await verifyContract()) return;
      const result = await registrationApi.requestRegistration(email);
      if (!alive.current) return;
      if (result.ok) {
        submitted.current = email;
        setSent(true); focusDone(); return;
      }
      if (result.status === 404) { setUnavailable(true); return; }
      if (result.status === 422 && result.code === 'invalid_email') {
        setError('Enter an email address like name@example.com.'); emailRef.current?.focus(); return;
      }
      setNotice(result.status === 429
        ? { tone: 'error', title: 'Too many requests', body: waitPhrase(result.retryAfter) }
        : { tone: 'error', title: 'Your request could not be completed', body: 'Wait a moment, then try again.' });
      focusNotice();
    });
  };
  useEffect(() => { alive.current = true; return () => { alive.current = false; submitted.current = ''; }; }, []);
  if (unavailable) return <UnavailablePage note={NOTE_CREATE} />;
  return <AccountLayout title="Create an account" note={NOTE_CREATE}
      lede="Start with your email. Choose a password after opening your account link.">
    {notice && <Notice notice={notice} focusRef={noticeRef} />}
    {sent ? <div className="acct-state" ref={doneRef} tabIndex={-1} data-testid="registration-check-email">
      <p className="acct-state-title">Check your email</p>
      <p>If an email arrives, follow its guidance. Delivery can take a few minutes; check your spam folder too.</p>
      <p>A finish-registration link works once and expires after 24 hours. Opening it does not create an account or sign you in.</p>
      <div className="acct-actions">
        <button className="acct-button acct-button-secondary" type="button" disabled={pending} onClick={() => send(true)}>
          {pending ? 'Sending request…' : 'Send again'}
        </button>
        <button className="acct-link" type="button" disabled={pending} onClick={() => { setSent(false); setNotice(null); submitted.current = ''; }}>Use another address</button>
      </div>
      <p><Link href={ACCOUNT_PATHS.signIn} className="acct-link">Sign in</Link> or <Link href={ACCOUNT_PATHS.recovery} className="acct-link">use account recovery</Link> when available.</p>
    </div> : <form className="acct-form" noValidate aria-labelledby="acct-title" onSubmit={event => { event.preventDefault(); send(false); }}>
      <Field id="email" label="Email" kind="email" autoComplete="email" inputRef={emailRef} error={error} />
      <div className="acct-actions acct-actions-form"><SubmitButton pending={pending} idle="Continue with email" busy="Sending request…" /></div>
    </form>}
  </AccountLayout>;
}

function RegistrationConfirmPage() {
  const alive = useRef(true);
  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);
  const [stage, setStage] = useState<'form' | 'invalid' | 'done' | 'unavailable' | 'conflict'>('form');
  const [notice, setNotice] = useState<NoticeData | null>(null);
  const [errors, setErrors] = useState<{ password?: string | null; confirm?: string | null }>({});
  const passwordRef = useRef<HTMLInputElement>(null);
  const confirmRef = useRef<HTMLInputElement>(null);
  const [noticeRef, focusNotice] = useFocusOn<HTMLDivElement>();
  const [stateRef, focusState] = useFocusOn<HTMLDivElement>();
  const { pending, run } = usePendingGuard();
  const { initial, credential, isCurrent } = useCredential(useCallback((next: Credential) => {
    for (const ref of [passwordRef, confirmRef]) if (ref.current) ref.current.value = '';
    setErrors({}); setNotice(null); setStage(next.token ? 'form' : 'invalid');
  }, []));
  useEffect(() => {
    if (!initial) setStage('invalid');
  }, [initial, credential]);
  const authority = useRegistrationContract(Boolean(credential.current.token), 'email-first-v1');
  const submit = (event: FormEvent) => {
    event.preventDefault();
    void run(async () => {
      const password = passwordRef.current?.value ?? '';
      const confirm = confirmRef.current?.value ?? '';
      const problems = { password: newPasswordProblem(password),
        confirm: !confirm ? 'Enter the password again.' : confirm !== password ? 'The passwords do not match.' : null };
      setErrors(problems); setNotice(null);
      if (problems.password || problems.confirm) {
        focusFirstInvalid([[problems.password, passwordRef], [problems.confirm, confirmRef]]); return;
      }
      const taken = credential.current;
      if (!taken.token) { setStage('invalid'); focusState(); return; }
      if (!await authority.verify()) return;
      if (!alive.current || !isCurrent(taken)) return;
      const result = await registrationApi.confirmRegistration(taken.token, password);
      if (!alive.current || !isCurrent(taken)) return;
      if (result.ok || result.status === 400 || result.status === 409 || result.status === 404) {
        credential.current = { token: null, generation: taken.generation };
        for (const ref of [passwordRef, confirmRef]) if (ref.current) ref.current.value = '';
        setStage(result.ok ? 'done' : result.status === 409 ? 'conflict' : result.status === 404 ? 'unavailable' : 'invalid');
        focusState(); return;
      }
      if (result.status === 422) { setErrors({ password: 'Use 12–256 characters.' }); passwordRef.current?.focus(); return; }
      setNotice(result.status === 429
        ? { tone: 'error', title: 'Too many attempts', body: waitPhrase(result.retryAfter) }
        : result.code === 'registration_pending'
          ? { tone: 'info', title: 'Your link is still being prepared', body: 'Wait a moment, then try again with this link.' }
          : result.code === 'busy'
            ? { tone: 'info', title: 'Please try again shortly', body: 'Kalillac is busy. Your link is kept here so you can try again.' }
            : { tone: 'error', title: 'Registration is temporarily unavailable',
                body: 'Try again with this link. If you may have completed setup, try signing in or use recovery when available.' });
      focusNotice();
    });
  };
  if (credential.current.token && stage !== 'invalid' && authority.contract !== 'email-first-v1')
    return <RegistrationAuthorityState loading={authority.loading} retry={authority.reload} />;
  if (stage === 'unavailable') return <UnavailablePage note={NOTE_CREATE} />;
  return <AccountLayout title="Finish creating your account" note={NOTE_CREATE}
      lede="Choose a password for the email address proved by your link.">
    {stage === 'done' ? <div className="acct-state acct-state-success" ref={stateRef} tabIndex={-1} data-testid="registration-complete">
      <p className="acct-state-title">Your account is ready.</p><p>You have not been signed in. Continue to sign in with your email and new password.</p>
      <Link href={ACCOUNT_PATHS.signIn} className="acct-button acct-button-primary">Continue to sign in</Link>
    </div> : stage === 'conflict' ? <div className="acct-state" ref={stateRef} tabIndex={-1}>
      <p>This account is already registered. No new account was created by this attempt.</p>
      <Link href={ACCOUNT_PATHS.signIn} className="acct-link">Sign in</Link>
    </div> : stage === 'invalid' ? <div className="acct-state" ref={stateRef} tabIndex={-1} data-testid="registration-invalid">
      <p>This link is invalid, expired, replaced or already used.</p>
      <Link href={ACCOUNT_PATHS.create} className="acct-button acct-button-primary">Request a new link</Link>
    </div> : <>
      {notice && <Notice notice={notice} focusRef={noticeRef} />}
      <form className="acct-form" noValidate aria-labelledby="acct-title" onSubmit={submit}>
        <input type="text" name="username" autoComplete="username" hidden readOnly tabIndex={-1} aria-hidden="true" />
        <Field id="new-password" label="Password" kind="password" autoComplete="new-password" inputRef={passwordRef} error={errors.password} hint="Use 12–256 characters." />
        <Field id="confirm-password" label="Confirm password" kind="password" autoComplete="new-password" inputRef={confirmRef} error={errors.confirm} />
        <div className="acct-actions acct-actions-form"><SubmitButton pending={pending} idle="Create account" busy="Creating account…" /></div>
      </form>
    </>}
  </AccountLayout>;
}

/* Backend contract authority is requested on entry and again before each POST. */
function useRegistrationContract(enabled = true, required?: registrationApi.RegistrationContract) {
  const [contract, setContract] = useState<registrationApi.RegistrationContract | null>(null);
  const [loading, setLoading] = useState(enabled);
  const generation = useRef(0);
  const alive = useRef(false);
  const reload = useCallback(async () => {
    const current = ++generation.current;
    setContract(null); setLoading(true);
    const next = await registrationApi.registrationContract();
    if (!alive.current || current !== generation.current) return;
    setContract(next && (!required || next === required) ? next : null);
    setLoading(false);
  }, [required]);
  useEffect(() => {
    alive.current = true;
    if (enabled) void reload();
    const restored = (event: PageTransitionEvent) => { if (event.persisted && enabled) void reload(); };
    window.addEventListener('pageshow', restored);
    return () => { alive.current = false; ++generation.current; window.removeEventListener('pageshow', restored); };
  }, [enabled, reload]);
  const verify = async () => {
    const current = generation.current;
    const next = await registrationApi.registrationContract();
    if (!alive.current || current !== generation.current) return false;
    if (!contract || next !== contract || (required && next !== required)) {
      ++generation.current; setContract(null); setLoading(false); return false;
    }
    return true;
  };
  return { contract, loading, reload: () => void reload(), verify };
}

function RegistrationAuthorityState({ loading, retry }: { loading: boolean; retry: () => void }) {
  if (!loading) return <UnavailablePage note={NOTE_CREATE} retry={retry} />;
  return <AccountLayout title="Create an account" note={NOTE_CREATE}>
    <Pending label="Checking registration availability..." />
  </AccountLayout>;
}

function AuthoritativeRegistrationPage() {
  const authority = useRegistrationContract();
  // A customer already known to be signed in is never shown registration.
  const { presence } = useCustomerSession();
  if (presence === 'signed-in') return <Redirect to={ACCOUNT_PATHS.overview} replace />;
  if (!authority.contract) return <RegistrationAuthorityState loading={authority.loading} retry={authority.reload} />;
  return authority.contract === 'legacy-v1'
    ? <CreateAccountPage verifyContract={authority.verify} />
    : <CreateEmailFirstPage verifyContract={authority.verify} />;
}
