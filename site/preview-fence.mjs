// DEVELOPMENT PREVIEW ONLY: the Vite process's network fence (vite.preview.config.ts installs it; it is never part
// of a build). Vite is a Node process of its own, so the preview's Python fence (candidate/tools/preview_guard.py)
// cannot reach it.
//
// Every outgoing TCP connection Node makes goes through net.Socket.prototype.connect (http, the dev server's
// /api and /__local__ proxy, WebSocket clients). A connection is refused, before it is attempted, when its port is
// one of local staging's (8010, 8011, 55433, under any host name) or its host is not this machine's loopback.
// Unix sockets and Windows pipes (local IPC) are not network connections and are left alone.
//
// KALILLAC_PREVIEW_FENCE_EXTRA_PORTS (comma-separated) adds ports; the tests use it for stand-in services.
import net from 'node:net';

export const PROTECTED_PORTS = Object.freeze([8010, 8011, 55433]);
const FENCED = Symbol.for('kalillac.previewFence');

export function protectedPorts(env = process.env) {
  const extra = String(env.KALILLAC_PREVIEW_FENCE_EXTRA_PORTS || '')
    .split(',').map((s) => s.trim()).filter((s) => /^\d+$/.test(s)).map(Number);
  return new Set([...PROTECTED_PORTS, ...extra]);
}

function isLoopback(host) {
  const h = String(host ?? 'localhost').trim().replace(/^\[|\]$/g, '').toLowerCase();
  if (h === 'localhost' || h === 'localhost.' || h === '::1') return true;
  if (/^127\.\d{1,3}\.\d{1,3}\.\d{1,3}$/.test(h)) return true;
  return h === '::ffff:127.0.0.1' || /^::ffff:127\./.test(h);
}

export function check(host, port) {
  const number = Number(port);
  if (!Number.isInteger(number)) return `an unknown port (${String(port)})`;
  if (protectedPorts().has(number)) return `local staging's port ${number}`;
  if (!isLoopback(host)) return `a host outside this machine (${String(host)})`;
  return null;
}

// The options a Socket.connect call carries, however it was called (net.connect passes a normalized array).
function target(args) {
  let first = args[0];
  if (Array.isArray(first)) first = first[0];
  if (first !== null && typeof first === 'object') {
    return first.path ? null : { host: first.host ?? 'localhost', port: first.port };
  }
  if (typeof first === 'string' && !/^\d+$/.test(first)) return null;     // A pipe or Unix socket path.
  return { host: typeof args[1] === 'string' ? args[1] : 'localhost', port: first };
}

export function installPreviewFence() {
  const proto = net.Socket.prototype;
  if (proto.connect[FENCED]) return;
  const real = proto.connect;
  function connect(...args) {
    const where = target(args);
    const refused = where && check(where.host, where.port);
    if (refused) {
      const error = Object.assign(new Error(`the development preview refused a connection to ${refused}`),
                                  { code: 'EKALILLACPREVIEWFENCE' });
      process.nextTick(() => this.destroy(error));
      return this;
    }
    return real.apply(this, args);
  }
  connect[FENCED] = true;
  proto.connect = connect;
}
