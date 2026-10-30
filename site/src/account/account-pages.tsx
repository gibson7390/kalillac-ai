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
import { Link, useLocation } from 'wouter';
import NotFound from '@/pages/not-found';
import * as api from './account-api';
import { ACCOUNT_PATHS } from './account-routes';
import './account.css';

/* --- page-lifetime memory ---------------------------------------------------- */

type NoticeData = { tone: 'error' | 'success' | 'info'; title: string; body?: ReactNode };

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

/* --- small helpers ------------------------------------------------------------------ */

const PASSWORD_MIN = 12;
const PASSWORD_MAX = 256;
const EMAIL_MAX = 320;
// The same check the server applies after trimming and lowercasing.
const EMAIL_SHAPE = /^[^@\s]+@[^@\s]+\.[^@\s]+$/;
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
            <h2 id={noteId}>{note.title}</h2>
            <p>{note.body}</p>
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
      <p className="acct-notice-title" id={id}>{notice.title}</p>
      {notice.body && <div className="acct-notice-body">{notice.body}</div>}
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
function UnavailablePage({ feature = 'accounts', note }: { feature?: UnavailableFeature; note: Note }) {
  const title = feature === 'accounts' ? 'Accounts aren’t available here yet'
    : feature === 'recovery' ? 'Password reset isn’t available right now'
      : 'Email verification isn’t available right now';
  return (
    <AccountLayout title={title} note={note}>
      <div className="acct-state acct-state-unavailable" data-testid="account-unavailable">
        <p>Kalillac works without an account. Temporary chat remains available.</p>
        <div className="acct-actions"><a className="acct-button acct-button-primary" href="/#chat">Start a private session</a></div>
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
        ? { tone: 'error', title: 'That email and password don’t match',
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

function CreateAccountPage() {
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

function AccountOverviewPage() {
  const [, navigate] = useLocation();
  const { session, reload } = useSession();
  const [plan, setPlan] = useState<Loadable<api.Entitlements>>({ state: 'loading' });
  const [billing, setBilling] = useState<Loadable<api.BillingStatus>>({ state: 'loading' });

  const sessionEnded = useCallback(() => {
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

  useEffect(() => {
    if (session.state === 'signed-out') navigate(ACCOUNT_PATHS.signIn, { replace: true });
    if (session.state === 'signed-in') { void loadPlan(); void loadBilling(); }
  }, [session, navigate, loadPlan, loadBilling]);

  let body: ReactNode;
  let lede: ReactNode;
  if (session.state === 'loading' || session.state === 'signed-out') body = <Pending label="Loading your account…" />;
  else if (session.state === 'unavailable') return <UnavailablePage note={NOTE_OVERVIEW} />;
  else if (session.state === 'error') body = <Unreachable onRetry={() => void reload()} />;
  else {
    lede = 'Your sign-in, verification, plan and billing settings, in one place.';
    body = <Overview user={session.user} plan={plan} billing={billing} onRetryPlan={() => void loadPlan()}
                     onRetryBilling={() => void loadBilling()} onSessionEnded={sessionEnded} />;
  }

  return (
    <AccountLayout title="Your account" lede={lede} note={NOTE_OVERVIEW}>
      {body}
    </AccountLayout>
  );
}

function Overview({ user, plan, billing, onRetryPlan, onRetryBilling, onSessionEnded }: {
  user: api.AccountUser; plan: Loadable<api.Entitlements>; billing: Loadable<api.BillingStatus>;
  onRetryPlan: () => void; onRetryBilling: () => void; onSessionEnded: () => void;
}) {
  const [, navigate] = useLocation();
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
      carryNotice({ tone: 'info', title: 'You’re signed out',
        body: 'Temporary chat works the same whether or not you’re signed in.' });
      navigate(ACCOUNT_PATHS.signIn);
      return;
    }
    setSignOutNotice(UNREACHABLE);
    focusSignOut();
  });

  return (
    <div className="acct-overview">
      <section className="acct-identity" aria-labelledby="acct-details-heading" data-tier={tier ?? undefined}>
        <h2 id="acct-details-heading" className="sr-only">Account details</h2>
        <div className="acct-identity-main">
          <p className="acct-identity-email" data-testid="account-email">{user.email}</p>
          {memberSince && <p className="acct-identity-meta">Member since <span data-testid="account-member-since">{memberSince}</span></p>}
        </div>
        <p className="acct-plan" data-testid="account-plan">
          {plan.state === 'ready' ? (plan.value.tier === 'paid' ? 'Paid plan' : 'Free account')
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

      {billing.state === 'error' && (
        <section className="acct-section" aria-labelledby="acct-billing-heading">
          <h2 id="acct-billing-heading" className="acct-section-title">Plan and billing</h2>
          <p className="acct-inline-problem">Billing details couldn’t be loaded. <button type="button" className="acct-text-button" onClick={onRetryBilling}>Try again</button></p>
        </section>
      )}
      {billing.state === 'ready' && plan.state === 'ready' && (
        <BillingSection status={billing.value} tier={plan.value.tier} emailVerified={user.email_verified}
                        onSessionEnded={onSessionEnded} />
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
    </div>
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

function BillingSection({ status, tier, emailVerified, onSessionEnded }: {
  status: api.BillingStatus; tier: api.Entitlements['tier']; emailVerified: boolean | undefined; onSessionEnded: () => void;
}) {
  const [notice, setNotice] = useState<NoticeData | null>(null);
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
      <h2 id="acct-billing-heading" className="acct-section-title">Plan and billing</h2>
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
      {tier === 'free' && (needsVerification
        ? <p className="acct-section-text" data-testid="billing-needs-verification">Verify your email address before starting a paid plan.</p>
        : <p className="acct-section-text">Upgrading opens Stripe’s secure checkout. Paid access is added after Stripe confirms the payment, which can take a few minutes.</p>)}
      {notice && <Notice notice={notice} focusRef={noticeRef} />}
      {(status.portal_available || (tier === 'free' && !needsVerification)) && (
        <div className="acct-actions">
          {tier === 'free' && !needsVerification && (
            <button type="button" className="acct-button acct-button-primary" aria-disabled={checkout.pending || undefined}
                    onClick={go(checkout, api.startCheckout)} data-testid="button-upgrade">
              {checkout.pending && <Spinner />}{checkout.pending ? 'Opening checkout…' : 'Upgrade to the paid plan'}
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
    case ACCOUNT_PATHS.create: return <CreateAccountPage />;
    case ACCOUNT_PATHS.overview: return <AccountOverviewPage />;
    case ACCOUNT_PATHS.recovery: return <RecoveryRequestPage />;
    case ACCOUNT_PATHS.recoveryConfirm: return <RecoveryConfirmPage />;
    case ACCOUNT_PATHS.verify: return <VerifyEmailPage />;
    default: return <NotFound />;
  }
}
