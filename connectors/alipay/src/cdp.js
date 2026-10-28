'use strict';
// Minimal Chrome DevTools Protocol client built on Node's global fetch and
// WebSocket (Node >= 22). No third-party dependency, no playwright.
const { CliError, codes } = require('./errors');

async function httpJson(url, { method = 'GET', timeoutMs = 3000 } = {}) {
  const res = await fetch(url, { method, signal: AbortSignal.timeout(timeoutMs) });
  if (!res.ok) throw new Error(`HTTP ${res.status} for ${method} ${url}`);
  const text = await res.text();
  if (!text.trim()) return null;
  return JSON.parse(text);
}

async function discover(port, { timeoutMs = 1500 } = {}) {
  try {
    const info = await httpJson(`http://127.0.0.1:${port}/json/version`, { timeoutMs });
    if (!info || !info.webSocketDebuggerUrl) return null;
    return info;
  } catch {
    return null;
  }
}

async function listTargets(port, { timeoutMs = 3000 } = {}) {
  try {
    const list = await httpJson(`http://127.0.0.1:${port}/json/list`, { timeoutMs });
    return Array.isArray(list) ? list : [];
  } catch {
    return [];
  }
}

const EVENT_WAIT_DEFAULT_MS = 30000;

class CDP {
  #ws;
  #nextId = 1;
  #pending = new Map();
  #handlers = new Map();
  #closed = false;
  #closeReason = null;

  constructor(ws) {
    this.#ws = ws;
    ws.addEventListener('message', (ev) => this.#onMessage(ev));
    ws.addEventListener('close', () => this.#onClose('socket closed'));
    ws.addEventListener('error', () => this.#onClose('socket error'));
  }

  static async connect(wsUrl, { timeoutMs = 10000 } = {}) {
    if (typeof WebSocket !== 'function') throw new CliError(codes.INTERNAL, 'global WebSocket unavailable (need Node >= 22)');
    const ws = new WebSocket(wsUrl);
    await new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new CliError(codes.BROWSER_DOWN, `CDP connect timeout: ${wsUrl}`)), timeoutMs);
      const done = (err) => {
        clearTimeout(timer);
        ws.removeEventListener('open', onOpen);
        ws.removeEventListener('error', onErr);
        if (err) reject(err);
        else resolve();
      };
      const onOpen = () => done(null);
      const onErr = (ev) => done(new CliError(codes.BROWSER_DOWN, `CDP connect failed: ${wsUrl}`, { error: String(ev?.message || ev?.type || 'error') }));
      ws.addEventListener('open', onOpen, { once: true });
      ws.addEventListener('error', onErr, { once: true });
    });
    return new CDP(ws);
  }

  get closed() {
    return this.#closed;
  }

  get closeReason() {
    return this.#closeReason;
  }

  #onMessage(ev) {
    let msg;
    try {
      msg = JSON.parse(typeof ev.data === 'string' ? ev.data : Buffer.from(ev.data).toString('utf8'));
    } catch {
      return;
    }
    if (msg.id !== undefined) {
      const entry = this.#pending.get(msg.id);
      if (!entry) return;
      this.#pending.delete(msg.id);
      if (msg.error) {
        const err = new CliError(codes.INTERNAL, `CDP ${entry.method} failed: ${msg.error.message}`, {
          method: entry.method,
          cdpCode: msg.error.code,
          data: msg.error.data,
        });
        entry.reject(err);
      } else {
        entry.resolve(msg.result);
      }
      return;
    }
    if (msg.method) {
      const hs = this.#handlers.get(msg.method);
      if (!hs) return;
      for (const h of [...hs]) {
        try {
          h(msg.params, msg.sessionId);
        } catch {
          /* listener errors must not break the socket */
        }
      }
    }
  }

  #onClose(reason) {
    if (this.#closed) return;
    this.#closed = true;
    this.#closeReason = reason;
    for (const [, entry] of this.#pending) {
      entry.reject(new CliError(codes.BROWSER_DOWN, `CDP connection closed during ${entry.method}`));
    }
    this.#pending.clear();
  }

  send(method, params = {}, { timeoutMs = 30000 } = {}) {
    if (this.#closed) return Promise.reject(new CliError(codes.BROWSER_DOWN, `CDP connection closed (${this.#closeReason})`));
    const id = this.#nextId++;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.#pending.delete(id);
        reject(new CliError(codes.INTERNAL, `CDP ${method} timed out after ${timeoutMs}ms`));
      }, timeoutMs);
      this.#pending.set(id, {
        method,
        resolve: (v) => {
          clearTimeout(timer);
          resolve(v);
        },
        reject: (e) => {
          clearTimeout(timer);
          reject(e);
        },
      });
      try {
        this.#ws.send(JSON.stringify({ id, method, params }));
      } catch (err) {
        clearTimeout(timer);
        this.#pending.delete(id);
        reject(new CliError(codes.BROWSER_DOWN, `CDP send failed: ${err.message}`));
      }
    });
  }

  on(method, handler) {
    if (!this.#handlers.has(method)) this.#handlers.set(method, new Set());
    this.#handlers.get(method).add(handler);
    return () => this.off(method, handler);
  }

  off(method, handler) {
    const hs = this.#handlers.get(method);
    if (hs) hs.delete(handler);
  }

  // Resolve on the next matching event (or reject on timeout).
  waitFor(method, predicate = () => true, { timeoutMs = EVENT_WAIT_DEFAULT_MS } = {}) {
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.off(method, handler);
        reject(new CliError(codes.INTERNAL, `timed out waiting for CDP event ${method}`));
      }, timeoutMs);
      const handler = (params) => {
        let match;
        try {
          match = predicate(params);
        } catch {
          match = false;
        }
        if (!match) return;
        clearTimeout(timer);
        this.off(method, handler);
        resolve(params);
      };
      this.on(method, handler);
    });
  }

  close() {
    try {
      this.#ws.close();
    } catch {
      /* ignore */
    }
    this.#onClose('closed by client');
  }
}

module.exports = { CDP, discover, listTargets, httpJson, EVENT_WAIT_DEFAULT_MS };
