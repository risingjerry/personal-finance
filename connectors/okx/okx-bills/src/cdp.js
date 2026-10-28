'use strict';
// Minimal Chrome DevTools Protocol client (Node >= 22 global fetch + WebSocket).
// No third-party dependency, no playwright.
const { CliError, codes } = require('./errors');

async function httpJson(url, { timeoutMs = 3000 } = {}) {
  const res = await fetch(url, { signal: AbortSignal.timeout(timeoutMs) });
  if (!res.ok) throw new Error(`HTTP ${res.status} for ${url}`);
  const text = await res.text();
  return text.trim() ? JSON.parse(text) : null;
}

async function discover(port, { timeoutMs = 1500 } = {}) {
  try {
    const info = await httpJson(`http://127.0.0.1:${port}/json/version`, { timeoutMs });
    return info && info.webSocketDebuggerUrl ? info : null;
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

class CDP {
  constructor(ws) {
    this.ws = ws;
    this.nextId = 1;
    this.pending = new Map();
    this.handlers = new Set();
    this.closed = false;
    ws.addEventListener('message', (ev) => this.#onMessage(ev));
    ws.addEventListener('close', () => { this.closed = true; });
    ws.addEventListener('error', () => { this.closed = true; });
  }

  static async connect(wsUrl, { timeoutMs = 10000 } = {}) {
    if (typeof WebSocket !== 'function') throw new CliError(codes.INTERNAL, 'global WebSocket unavailable (need Node >= 22)');
    const ws = new WebSocket(wsUrl);
    await new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new CliError(codes.BROWSER_DOWN, `CDP connect timeout: ${wsUrl}`)), timeoutMs);
      const done = (err) => { clearTimeout(timer); err ? reject(err) : resolve(); };
      ws.addEventListener('open', () => done(null), { once: true });
      ws.addEventListener('error', () => done(new CliError(codes.BROWSER_DOWN, `CDP connect failed: ${wsUrl}`)), { once: true });
    });
    return new CDP(ws);
  }

  #onMessage(ev) {
    let msg;
    try { msg = JSON.parse(typeof ev.data === 'string' ? ev.data : String(ev.data)); } catch { return; }
    if (msg.id !== undefined) {
      const entry = this.pending.get(msg.id);
      if (!entry) return;
      this.pending.delete(msg.id);
      clearTimeout(entry.timer);
      if (msg.error) entry.reject(new CliError(codes.INTERNAL, `${entry.method} failed: ${msg.error.message}`, { method: entry.method }));
      else entry.resolve(msg.result);
      return;
    }
    for (const h of this.handlers) {
      try { h(msg); } catch { /* handler errors must not kill the socket */ }
    }
  }

  on(fn) {
    this.handlers.add(fn);
    return () => this.handlers.delete(fn);
  }

  send(method, params = {}, { sessionId, timeoutMs = 30000 } = {}) {
    if (this.closed) return Promise.reject(new CliError(codes.BROWSER_DOWN, 'CDP socket closed'));
    const id = this.nextId++;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new CliError(codes.INTERNAL, `CDP timeout: ${method}`, { method, timeoutMs }));
      }, timeoutMs);
      this.pending.set(id, { resolve, reject, method, timer });
      const payload = { id, method, params };
      if (sessionId) payload.sessionId = sessionId;
      this.ws.send(JSON.stringify(payload));
    });
  }

  close() {
    try { this.ws.close(); } catch { /* ignore */ }
  }
}

module.exports = { CDP, discover, listTargets, httpJson };
