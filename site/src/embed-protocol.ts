/* Homepage <-> /app/ handoff protocol (parent side).

   The homepage frames the canonical chat workspace (/app/, ../frontend) in a
   same-origin iframe. The two windows exchange exactly two messages, and
   neither carries any prompt or conversation text:

   - child -> parent  { type: PROMPT_SUBMITTED, version: 1 }
       The chat is submitting a valid prompt while it is shown as the compact
       homepage card. The chat sends the prompt itself, exactly once; this
       message only asks the parent to present it full-screen.
   - parent -> child  { type: PRESENTATION, version: 1, expanded: boolean }
       The parent's current presentation, so the chat can adjust its chrome.

   Both sides accept a message only from the exact same origin, only from the
   exact expected window, and only with the exact type, version and keys.
   Messages are always posted to the exact origin, never to "*". The same
   constants are defined in ../frontend/app.js. */

export const EMBED_PROTOCOL_VERSION = 1;
export const PROMPT_SUBMITTED = 'kalillac:embed:prompt-submitted';
export const PRESENTATION = 'kalillac:embed:presentation';

// The canonical chat workspace. A real load of this URL (direct visit or
// refresh) is served by ../frontend, never by this site.
export const CHAT_PATH = '/app/';

function isPlainRecord(value: unknown): value is Record<string, unknown> {
  return (
    typeof value === 'object' &&
    value !== null &&
    Object.getPrototypeOf(value) === Object.prototype
  );
}

function hasExactKeys(value: Record<string, unknown>, keys: string[]): boolean {
  const actual = Object.keys(value).sort();
  const expected = [...keys].sort();
  return actual.length === expected.length && actual.every((key, i) => key === expected[i]);
}

/** True only for a prompt-submitted message from the framed chat window. */
export function isPromptSubmittedMessage(
  event: MessageEvent,
  frameWindow: Window | null,
  origin: string,
): boolean {
  if (event.origin !== origin) return false;
  if (frameWindow === null || event.source !== frameWindow) return false;

  const data: unknown = event.data;

  return (
    isPlainRecord(data) &&
    hasExactKeys(data, ['type', 'version']) &&
    data.type === PROMPT_SUBMITTED &&
    data.version === EMBED_PROTOCOL_VERSION
  );
}

/** Tell the framed chat whether it is presented full-screen. */
export function postPresentation(frameWindow: Window | null, origin: string, expanded: boolean): void {
  if (frameWindow === null) return;

  frameWindow.postMessage(
    { type: PRESENTATION, version: EMBED_PROTOCOL_VERSION, expanded },
    origin,
  );
}
