'use strict';
// Chrome lifecycle for the dedicated okx-cli profile + a thin page handle.
// Mechanism only: no OKX URLs or page semantics here.
const fs = require('node:fs');
const path = require('node:path');
const { spawn } = require('node:child_process');
const { CDP, discover, listTargets, httpJson } = require('./cdp');
const { CliError, codes } = require('./errors');
const { layout, ensureLayout, sleep } = require('./util');

const DEFAULT_PORT = 9340;
const DEFAULT_CHROME = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';

function chromePath() {
  return process.env.OKX_CLI_CHROME || DEFAULT_CHROME;
}

function defaultPort() {
  const n = Number(process.env.OKX_CDP_PORT || DEFAULT_PORT);
  return Number.isInteger(n) && n > 0 ? n : DEFAULT_PORT;
}

async function portInUse(port) {
  try {
    await httpJson(`http://127.0.0.1:${port}/json/version`, { timeoutMs: 800 });
    return true;
  } catch {
    return false;
  }
}

class PageHandle {
  constructor(conn, sessionId, targetId) {
    this.conn = conn;
    this.sessionId = sessionId;
    this.targetId = targetId;
  }

  send(method, params = {}, opts = {}) {
    return this.conn.send(method, params, { ...opts, sessionId: this.sessionId });
  }

  async evaluate(expression, { awaitPromise = true, timeoutMs = 45000 } = {}) {
    const res = await this.send('Runtime.evaluate', {
      expression, awaitPromise, returnByValue: true, userGesture: true,
    }, { timeoutMs: timeoutMs + 5000 });
    if (res && res.exceptionDetails) {
      const d = res.exceptionDetails;
      throw new CliError(codes.INTERNAL, `page eval error: ${String(d.exception?.description || d.text).split('\n')[0]}`);
    }
    return res ? res.result?.value : undefined;
  }

  async navigate(url, { timeoutMs = 45000 } = {}) {
    await this.send('Page.enable').catch(() => {});
    const res = await this.send('Page.navigate', { url }, { timeoutMs });
    if (res && res.errorText) throw new CliError(codes.BROWSER_DOWN, `navigation failed: ${res.errorText}`, { url });
    const deadline = Date.now() + 15000;
    while (Date.now() < deadline) {
      const state = await this.evaluate('document.readyState', { awaitPromise: false }).catch(() => null);
      if (state === 'complete' || state === 'interactive') return;
      await sleep(250);
    }
  }

  async url() {
    return this.evaluate('location.href', { awaitPromise: false });
  }

  async bodyText() {
    return this.evaluate("(document.body ? document.body.innerText : '').replace(/\\s+/g,' ').trim()", { awaitPromise: false });
  }

  async close() {
    try { await this.conn.send('Target.closeTarget', { targetId: this.targetId }); } catch { /* ignore */ }
  }
}

class BrowserSession {
  constructor({ home, port = defaultPort(), log = () => {}, chrome = chromePath() } = {}) {
    this.home = home;
    this.port = port;
    this.log = log;
    this.chrome = chrome;
    this.conn = null;
    this.launched = false;
  }

  layout() { return layout(this.home); }

  async isUp() {
    return !!(await discover(this.port, { timeoutMs: 1200 }));
  }

  async ensure({ launch = true } = {}) {
    const v = await discover(this.port, { timeoutMs: 1200 });
    if (v) {
      this.conn = this.conn && !this.conn.closed ? this.conn : await CDP.connect(v.webSocketDebuggerUrl);
      return { launched: false, version: v };
    }
    if (!launch) throw new CliError(codes.BROWSER_DOWN, `no Chrome on 127.0.0.1:${this.port}`, { port: this.port });
    await this.launch();
    const v2 = await discover(this.port, { timeoutMs: 4000 });
    if (!v2) throw new CliError(codes.BROWSER_DOWN, `Chrome did not expose CDP on ${this.port}`);
    this.conn = await CDP.connect(v2.webSocketDebuggerUrl);
    return { launched: true, version: v2 };
  }

  async launch() {
    const L = ensureLayout(this.home);
    if (!fs.existsSync(this.chrome)) {
      throw new CliError(codes.CHROME_MISSING, `Chrome binary not found: ${this.chrome}`, { chrome: this.chrome });
    }
    // A foreign Chrome already holding this port would make every later call
    // act on the wrong profile and interfere with an unrelated session.
    if (await portInUse(this.port)) {
      throw new CliError(codes.BROWSER_DOWN, `port ${this.port} already serves a browser that is not ours`, {
        port: this.port,
        hint: 'set OKX_CDP_PORT to a free port',
      });
    }
    const args = [
      `--user-data-dir=${L.profile}`,
      `--remote-debugging-port=${this.port}`,
      '--no-first-run',
      '--no-default-browser-check',
      '--window-position=60,60',
      '--window-size=1440,1000',
    ];
    if (process.env.OKX_HEADLESS === '1') args.push('--headless=new');
    this.log(`launching Chrome (port ${this.port}, profile ${L.profile})`);
    const child = spawn(this.chrome, args, { detached: true, stdio: 'ignore' });
    child.unref();
    const deadline = Date.now() + 25000;
    while (Date.now() < deadline) {
      await sleep(300);
      if (await discover(this.port, { timeoutMs: 800 })) { this.launched = true; return; }
      if (child.exitCode != null) throw new CliError(codes.BROWSER_DOWN, `Chrome exited during startup (${child.exitCode})`);
    }
    throw new CliError(codes.BROWSER_DOWN, `Chrome did not expose CDP on ${this.port} within 25s`);
  }

  // Prove the browser on our port really uses our profile: chrome://version
  // renders the profile path, and the probe tab is closed again immediately.
  async verifyOwnership() {
    if (!this.conn || this.conn.closed) await this.ensure({ launch: false });
    const { targetId } = await this.conn.send('Target.createTarget', { url: 'chrome://version' });
    try {
      const { sessionId } = await this.conn.send('Target.attachToTarget', { targetId, flatten: true });
      const page = new PageHandle(this.conn, sessionId, targetId);
      const want = path.resolve(this.layout().profile);
      const deadline = Date.now() + 10000;
      while (Date.now() < deadline) {
        const text = await page.bodyText().catch(() => '');
        if (text && text.length > 20) {
          if (!text.includes(want)) {
            throw new CliError(codes.BROWSER_DOWN, `browser on port ${this.port} uses a different profile`, { port: this.port, expected: want });
          }
          return true;
        }
        await sleep(300);
      }
      throw new CliError(codes.BROWSER_DOWN, 'could not read chrome://version for ownership check');
    } finally {
      await this.conn.send('Target.closeTarget', { targetId }).catch(() => {});
    }
  }

  async pages() {
    if (!this.conn || this.conn.closed) await this.ensure({ launch: false });
    const { targetInfos } = await this.conn.send('Target.getTargets');
    return targetInfos.filter((t) => t.type === 'page');
  }

  // Reuse an existing okx.com tab (never disturb the user's other tabs) or open one.
  async openPage({ url = 'about:blank', reuseOkx = true } = {}) {
    if (!this.conn || this.conn.closed) await this.ensure({ launch: false });
    let targetId = null;
    if (reuseOkx) {
      const pages = await this.pages();
      const hit = pages.find((t) => /okx\.com/.test(t.url || ''));
      if (hit) targetId = hit.targetId;
    }
    if (!targetId) ({ targetId } = await this.conn.send('Target.createTarget', { url: 'about:blank' }));
    const { sessionId } = await this.conn.send('Target.attachToTarget', { targetId, flatten: true });
    const page = new PageHandle(this.conn, sessionId, targetId);
    await page.send('Page.enable').catch(() => {});
    await page.send('Runtime.enable').catch(() => {});
    if (url && url !== 'about:blank') await page.navigate(url);
    return page;
  }

  async dispose() {
    if (this.conn) this.conn.close();
    this.conn = null;
  }
}

module.exports = { BrowserSession, PageHandle, defaultPort, chromePath, portInUse };
