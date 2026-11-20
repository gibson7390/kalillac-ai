/* Same-origin client for the account, recovery, verification, entitlement and
   billing HTTP contracts (candidate/kalillac_accounts and kalillac_billing).

   Every request goes to a fixed relative /api/account path with the browser's
   own same-origin cookie, asks for no caching, and sends JSON only where the
   contract requires a body. Responses are reduced to typed results: raw server
   text is never returned to the interface, and nothing here logs, stores or
   forwards a password, token or session value. */

export type ApiError = {
  ok: false;
  /* The HTTP status, or 0 when the request never completed. */
  status: number;
  /* The contract's error code when the response carried one. */
  code: string | null;
  /* Seconds from Retry-After on a 429, when present and sane. */
  retryAfter: number | null;
};

export type ApiResult<T> = { ok: true; status: number; data: T } | ApiError;

export type AccountUser = {
  email: string;
  created_at: string;
  /* Present only while email verification is enabled on the server. */
  email_verified?: boolean;
};

export type Entitlements = {
  tier: 'free' | 'paid';
};

export type BillingStatus = {
  subscription_status: string | null;
  cancel_at_period_end: boolean;
  current_period_end: string | null;
  portal_available: boolean;
};

const MAX_RETRY_AFTER = 24 * 60 * 60;

function retryAfterSeconds(response: Response): number | null {
  const value = response.headers.get('Retry-After');
  if (!value || !/^\d{1,6}$/.test(value.trim())) return null;
  const seconds = Number(value.trim());
  return seconds > 0 && seconds <= MAX_RETRY_AFTER ? seconds : null;
}

/* A response body is valid JSON, not advertised as JSON at all (an HTML route
   fallback or proxy page), or advertised as JSON but malformed. The three
   are kept apart: only the first two can be told from each other by status. */
type Body = { kind: 'json'; value: unknown } | { kind: 'not-json' } | { kind: 'malformed' };

async function readBody(response: Response): Promise<Body> {
  const type = response.headers.get('Content-Type') ?? '';
  if (!type.toLowerCase().startsWith('application/json')) return { kind: 'not-json' };
  try {
    return { kind: 'json', value: await response.json() };
  } catch {
    return { kind: 'malformed' };
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function errorCode(body: unknown): string | null {
  return isRecord(body) && typeof body.error === 'string' && /^[a-z_]{1,64}$/.test(body.error)
    ? body.error
    : null;
}

async function request(method: 'GET' | 'POST' | 'DELETE', path: string, body?: Record<string, unknown>):
    Promise<{ response: Response; json: unknown; body: Body } | ApiError> {
  const init: RequestInit = {
    method,
    credentials: 'same-origin',
    cache: 'no-store',
    redirect: 'error',
    headers: { Accept: 'application/json' },
  };
  if (body !== undefined) {
    init.headers = { Accept: 'application/json', 'Content-Type': 'application/json' };
    init.body = JSON.stringify(body);
  }
  let response: Response;
  try {
    response = await fetch(path, init);
  } catch {
    return { ok: false, status: 0, code: null, retryAfter: null };
  }
  const parsed = await readBody(response);
  // `json` is the parsed value, or undefined for any body that is not valid JSON.
  return { response, json: parsed.kind === 'json' ? parsed.value : undefined, body: parsed };
}

function failure(response: Response, json: unknown): ApiError {
  return {
    ok: false,
    status: response.status,
    code: errorCode(json),
    retryAfter: response.status === 429 || response.status === 503 ? retryAfterSeconds(response) : null,
  };
}

/* A request whose success is only the documented {"ok": true}. */
export async function acknowledge(method: 'POST', path: string, body?: Record<string, unknown>,
                           expected = 200): Promise<ApiResult<null>> {
  const result = await request(method, path, body);
  if ('ok' in result) return result;
  const { response, json } = result;
  if (response.status === expected && isRecord(json) && json.ok === true) {
    return { ok: true, status: response.status, data: null };
  }
  return failure(response, json);
}

function parseUser(json: unknown): AccountUser | null {
  if (!isRecord(json) || !isRecord(json.user)) return null;
  const { email, created_at: createdAt, email_verified: verified } = json.user;
  if (typeof email !== 'string' || typeof createdAt !== 'string') return null;
  const user: AccountUser = { email, created_at: createdAt };
  if (typeof verified === 'boolean') user.email_verified = verified;
  return user;
}

/* --- account ------------------------------------------------------------------- */

export type SessionResult =
  | { state: 'signed-in'; user: AccountUser }
  | { state: 'signed-out' }
  /* The account routes are not mounted: accounts are off on this server. */
  | { state: 'unavailable' }
  /* Anything else: network failure, server error or an unexpected body. */
  | { state: 'error' };

/* Exactly:
   - 404, JSON or not: the account routes are not mounted (unavailable);
   - 200 that is not JSON (a static route fallback): unavailable;
   - 200 with a valid user: signed in; 401 with the documented code: signed out;
   - anything else, including a non-JSON 5xx proxy page, malformed JSON, an
     unexpected JSON shape and a network failure: a retryable error. */
export async function getSession(): Promise<SessionResult> {
  const result = await request('GET', '/api/account/me');
  if ('ok' in result) return { state: 'error' };
  const { response, body } = result;
  if (response.status === 404) return { state: 'unavailable' };
  if (response.status === 200) {
    if (body.kind === 'not-json') return { state: 'unavailable' };
    const user = body.kind === 'json' ? parseUser(body.value) : null;
    return user ? { state: 'signed-in', user } : { state: 'error' };
  }
  if (response.status === 401 && body.kind === 'json' && errorCode(body.value) === 'not_authenticated') {
    return { state: 'signed-out' };
  }
  return { state: 'error' };
}

export async function register(email: string, password: string): Promise<ApiResult<null>> {
  const result = await request('POST', '/api/account/register', { email, password });
  if ('ok' in result) return result;
  const { response, json } = result;
  if (response.status === 201 && parseUser(json)) return { ok: true, status: 201, data: null };
  return failure(response, json);
}

export async function login(email: string, password: string): Promise<ApiResult<null>> {
  const result = await request('POST', '/api/account/login', { email, password });
  if ('ok' in result) return result;
  const { response, json } = result;
  if (response.status === 200 && parseUser(json)) return { ok: true, status: 200, data: null };
  return failure(response, json);
}

export function logout(): Promise<ApiResult<null>> {
  return acknowledge('POST', '/api/account/logout');
}

export async function getEntitlements(): Promise<ApiResult<Entitlements>> {
  const result = await request('GET', '/api/account/entitlements');
  if ('ok' in result) return result;
  const { response, json } = result;
  if (response.status === 200 && isRecord(json) && isRecord(json.entitlements)) {
    const tier = json.entitlements.tier;
    if (tier === 'free' || tier === 'paid') return { ok: true, status: 200, data: { tier } };
  }
  return failure(response, json);
}

/* --- account security (sessions, password, deletion request) --------------------- */

export type AccountSecurity = {
  sessions: boolean;
  password_change: boolean;
  deletion_request: boolean;
};

/* What this server lets a signed-in account manage. The route exists only
   where the server's account-security mode provides these operations; a 404
   means none of them is offered here, and the page shows none. */
export async function getAccountSecurity(): Promise<ApiResult<AccountSecurity>> {
  const result = await request('GET', '/api/account/security');
  if ('ok' in result) return result;
  const { response, json } = result;
  if (response.status === 200 && isRecord(json) && isRecord(json.security)) {
    const s = json.security;
    if (typeof s.sessions === 'boolean' && typeof s.password_change === 'boolean' &&
        typeof s.deletion_request === 'boolean') {
      return { ok: true, status: 200, data: {
        sessions: s.sessions, password_change: s.password_change, deletion_request: s.deletion_request,
      } };
    }
  }
  return failure(response, json);
}

/* Ends every other session of this account; this browser stays signed in. */
export function revokeOtherSessions(): Promise<ApiResult<null>> {
  return acknowledge('POST', '/api/account/sessions/revoke-others', {});
}

/* Ends every session of this account, this browser's included. */
export function revokeAllSessions(): Promise<ApiResult<null>> {
  return acknowledge('POST', '/api/account/sessions/revoke-all', {});
}

export function changePassword(currentPassword: string, newPassword: string): Promise<ApiResult<null>> {
  return acknowledge('POST', '/api/account/password',
    { current_password: currentPassword, new_password: newPassword });
}

/* Requests deletion of the account: every session ends and sign-in closes. */
export function requestAccountDeletion(currentPassword: string): Promise<ApiResult<null>> {
  return acknowledge('POST', '/api/account/deletion-request', { current_password: currentPassword });
}

/* --- recovery and verification ------------------------------------------------- */

export function requestRecovery(email: string): Promise<ApiResult<null>> {
  return acknowledge('POST', '/api/account/recovery/request', { email }, 202);
}

export function confirmRecovery(token: string, password: string): Promise<ApiResult<null>> {
  return acknowledge('POST', '/api/account/recovery/confirm', { token, password });
}

export function requestVerification(): Promise<ApiResult<null>> {
  return acknowledge('POST', '/api/account/email-verification/request', {}, 202);
}

export function confirmVerification(token: string): Promise<ApiResult<null>> {
  return acknowledge('POST', '/api/account/email-verification/confirm', { token });
}

/* --- billing ----------------------------------------------------------------------- */

export async function getBilling(): Promise<ApiResult<BillingStatus>> {
  const result = await request('GET', '/api/account/billing');
  if ('ok' in result) return result;
  const { response, json } = result;
  if (response.status === 200 && isRecord(json) && isRecord(json.billing)) {
    const b = json.billing;
    const status = typeof b.subscription_status === 'string' ? b.subscription_status : null;
    const end = typeof b.current_period_end === 'string' ? b.current_period_end : null;
    if (typeof b.cancel_at_period_end === 'boolean' && typeof b.portal_available === 'boolean') {
      return { ok: true, status: 200, data: {
        subscription_status: status, cancel_at_period_end: b.cancel_at_period_end,
        current_period_end: end, portal_available: b.portal_available,
      } };
    }
  }
  return failure(response, json);
}

/* Only exact HTTPS URLs on Stripe's own hosted pages are followed. */
export function trustedStripeUrl(value: unknown, host: 'checkout.stripe.com' | 'billing.stripe.com'): string | null {
  if (typeof value !== 'string' || value.length > 2048) return null;
  let url: URL;
  try {
    url = new URL(value);
  } catch {
    return null;
  }
  if (url.protocol !== 'https:' || url.hostname !== host || url.port !== '' ||
      url.username !== '' || url.password !== '') return null;
  return url.href;
}

async function billingRedirect(path: string, field: 'checkout_url' | 'portal_url',
                               host: 'checkout.stripe.com' | 'billing.stripe.com'): Promise<ApiResult<string>> {
  // The server chooses the Price, customer and every URL; no body is sent.
  const result = await request('POST', path);
  if ('ok' in result) return result;
  const { response, json } = result;
  if (response.status === 200 && isRecord(json)) {
    const url = trustedStripeUrl(json[field], host);
    if (url) return { ok: true, status: 200, data: url };
    return { ok: false, status: 200, code: 'untrusted_redirect', retryAfter: null };
  }
  return failure(response, json);
}

export function startCheckout(): Promise<ApiResult<string>> {
  return billingRedirect('/api/account/billing/checkout', 'checkout_url', 'checkout.stripe.com');
}

export function openBillingPortal(): Promise<ApiResult<string>> {
  return billingRedirect('/api/account/billing/portal', 'portal_url', 'billing.stripe.com');
}

/* --- the account's stored files (/api/files, a files deployment only) ---------------------------------------- */

export type StoredFile = {
  id: string;
  filename: string;
  size: number;
  /* When it stops being available (ISO 8601); after that it can only be deleted. */
  expires_at: string;
  available: boolean;
};

const FILE_ID = /^[A-Za-z0-9_-]{22}$/;

function parseFiles(json: unknown): StoredFile[] | null {
  if (!isRecord(json) || !Array.isArray(json.files)) return null;
  const files: StoredFile[] = [];
  for (const item of json.files) {
    if (!isRecord(item) || typeof item.id !== 'string' || !FILE_ID.test(item.id)
        || typeof item.filename !== 'string' || item.filename.length === 0 || item.filename.length > 255
        || typeof item.size !== 'number' || !Number.isSafeInteger(item.size) || item.size < 0
        || typeof item.expires_at !== 'string' || Number.isNaN(Date.parse(item.expires_at))
        || typeof item.available !== 'boolean') return null;
    files.push({ id: item.id, filename: item.filename, size: item.size, expires_at: item.expires_at,
                 available: item.available });
  }
  return files;
}

/* What Kalillac Premium would add for this signed-in Free account, as this deployment says: the reasoning
   modes only a verified Premium plan may use (the chat's own rule), and whether adding files needs Premium
   (the account may not add any now). Anything not clearly answered counts as not offered, so nothing is
   advertised that this deployment does not provide. */
export type PremiumCapabilities = { deepReasoning: boolean; files: boolean };

export async function getPremiumCapabilities(filesOffered: boolean): Promise<PremiumCapabilities> {
  const [modes, files] = await Promise.all([
    request('GET', '/api/chat/reasoning-modes'),
    filesOffered ? request('GET', '/api/files') : Promise.resolve(null),
  ]);
  const deepReasoning = modes !== null && !('ok' in modes) && modes.response.status === 200
    && isRecord(modes.json) && Array.isArray(modes.json.premium) && modes.json.premium.includes('deep');
  const filesNeedPremium = files !== null && !('ok' in files) && files.response.status === 200
    && isRecord(files.json) && files.json.can_add === false;
  return { deepReasoning, files: filesNeedPremium };
}

/* The signed-in account's files, expired ones included (they can only be deleted). */
export async function listFiles(): Promise<ApiResult<StoredFile[]>> {
  const result = await request('GET', '/api/files');
  if ('ok' in result) return result;
  const { response, json } = result;
  const files = response.status === 200 ? parseFiles(json) : null;
  return files ? { ok: true, status: 200, data: files } : failure(response, json);
}

/* Deletes one of the account's files from storage. */
export async function deleteFile(id: string): Promise<ApiResult<null>> {
  if (!FILE_ID.test(id)) return { ok: false, status: 0, code: null, retryAfter: null };
  const result = await request('DELETE', `/api/files/${id}`);
  if ('ok' in result) return result;
  const { response, json } = result;
  if (response.status === 200 && isRecord(json) && json.deleted === true) {
    return { ok: true, status: 200, data: null };
  }
  return failure(response, json);
}

/* The address a stored, still-available file downloads from (an attachment, never rendered). */
export function fileDownloadPath(id: string): string | null {
  return FILE_ID.test(id) ? `/api/files/${id}` : null;
}
