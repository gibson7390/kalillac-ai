/* Fixed V2 routes share the existing safe same-origin request client. */
import { acknowledge, type ApiResult } from './account-api';

export function requestRegistration(email: string): Promise<ApiResult<null>> {
  return acknowledge('POST', '/api/account/register', { email }, 202);
}

export function confirmRegistration(token: string, password: string): Promise<ApiResult<null>> {
  return acknowledge('POST', '/api/account/register/confirm', { token, password });
}


export type RegistrationContract = 'legacy-v1' | 'email-first-v1';

/** No stored mode: a unique challenge rejects obsolete cached responses. */
export async function registrationContract(): Promise<RegistrationContract | null> {
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), 5000);
  try {
    const nonce = Array.from(crypto.getRandomValues(new Uint8Array(16)),
      byte => byte.toString(16).padStart(2, '0')).join('');
    const response = await fetch('/api/account/registration-contract', {
      method: 'GET', credentials: 'same-origin', cache: 'no-store', redirect: 'error',
      headers: { 'Accept': 'application/json', 'X-Kalillac-Registration-Nonce': nonce },
      signal: controller.signal,
    });
    if (response.status !== 200 || !response.headers.get('Content-Type')?.toLowerCase().startsWith('application/json')
        || !response.headers.get('Cache-Control')?.toLowerCase().split(',').some(part => part.trim() === 'no-store')) return null;
    const body: unknown = await response.json();
    if (!body || typeof body !== 'object' || Array.isArray(body)) return null;
    const value = body as Record<string, unknown>;
    if (Object.keys(value).length !== 2 || value.nonce !== nonce
        || (value.contract !== 'legacy-v1' && value.contract !== 'email-first-v1')) return null;
    return value.contract;
  } catch { return null; }
  finally { window.clearTimeout(timeout); }
}
