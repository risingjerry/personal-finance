'use strict';
// Chrome lifecycle + CDP plumbing for the dedicated alipay-cli profile.
// Mechanism only: no Alipay-specific URLs or page semantics live here.
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawn } = require('node:child_process');
const { CDP, discover, listTargets } = require('./cdp');
const { CliError, codes } = require('./errors');
const { layout, ensureDir, ensureLayout, writeFileAtomic, nowIso, sleep, fileExists } = require('./util');

const DEFAULT_PORT = 9333;
const DEFAULT_CHROME = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';

function chromePath() {
  return process.env.ALIPAY_CLI_CHROME || DEFAULT_CHROME;
}

function defaultPort() {
  const n = Number(process.env.ALIPAY_CLI_CDP_PORT || DEFAULT_PORT);
  return Number.isInteger(n) && n > 0 ? n : DEFAULT_PORT;
}

// Cookie shapes accepted by Storage.setCookies.
function toInjectableCookie(c) {
  if (!c || !c.name || !c.domain) return null;
  // CHIPS (partitioned) cookies cannot be re-injected faithfully; skip them.
  if (c.name.startsWith('_CHIPS-')) return null;
  const out = {
    name: String(c.name),
    value: String(c.value ?? ''),
    domain: String(c.domain),
    path: String(c.path || '/'),
    secure: !!c.secure,
    httpOnly: !!c.httpOnly,
    // Session cookies use -1; keep absolute expiry otherwise.
    expires: typeof c.expires === 'number' && c.expires > 0 ? c.expires : -1,
  };
  if (c.sameSite && ['Strict', 'Lax', 'None'].includes(c.sameSite)) out.sameSite = c.sameSite;
  return out;
}

function sanitizeCookieForVault(c) {
  return toInjectableCookie(c);
}

class PageHandle {
  constructor({ targetId, conn, url, created = false }) {
    this.targetId = targetId;
    this.conn = conn;
    this.initialUrl = url || null;
    this.created = created;
    this.closed = false;
  }

  async send(method, params = {}, opts) {
    return this.conn.send(method, params, opts);
  }

  async navigate(url, { timeoutMs = 45000 } = {}) {
    await this.send('Page.enable', {}, { timeoutMs: 5000 }).catch(() => {});
    const loaded = this.conn.waitFor('Page.loadEventFired', () => true, { timeoutMs }).catch(() => null);
    const res = await this.send('Page.navigate', { url }, { timeoutMs });
    if (res && res.errorText) throw new CliError(codes.BROWSER_DOWN, `navigation failed: ${res.errorText}`, { url });
    await loaded;
    return res;
  }

  // Evaluate in the page main world. Retries through document swaps.
  async evaluate(expression, { awaitPromise = false, returnByValue = true, timeoutMs = 30000, retries = 3 } = {}) {
    let lastErr;
    for (let i = 0; i < retries; i++) {
      try {
        if (this.conn.closed) throw new CliError(codes.BROWSER_DOWN, 'CDP connection closed');
        const res = await this.send(
          'Runtime.evaluate',
          { expression, awaitPromise, returnByValue, userGesture: true, timeout: timeoutMs },
          { timeoutMs: timeoutMs + 5000 },
        );
        if (res && res.exceptionDetails) {
          const d = res.exceptionDetails;
          const msg = d.exception?.description || d.text || 'page evaluation failed';
          throw new CliError(codes.INTERNAL, `page eval error: ${msg.split('\n')[0]}`);
        }
        return res ? res.result?.value : undefined;
      } catch (err) {
        lastErr = err;
        const m = String(err.message || '');
        if (/Cannot find context|Execution context was destroyed|Inspected target navigated|Target closed/i.test(m)) {
          await sleep(150);
          continue;
        }
        throw err;
      }
    }
    throw lastErr;
  }

  async bodyText() {
    return this.evaluate("(document.body ? document.body.innerText : '').replace(/\\s+/g,' ').trim()");
  }

  async currentUrl() {
    return this.evaluate('location.href');
  }

  async close() {
    this.closed = true;
    try {
      this.conn.close();
    } catch {
      /* ignore */
    }
  }
}

class BrowserSession {
  constructor({ home, port = defaultPort(), log = () => {}, chrome = chromePath() } = {}) {
    this.home = home;
    this.port = port;
    this.log = log;
    this.chrome = chrome;
    this.pages = [];
    this._browserConn = null;
    this._version = null;
    this._launched = false;
  }

  async version({ timeoutMs = 1500 } = {}) {
    if (this._version) return this._version;
    const v = await discover(this.port, { timeoutMs });
    if (v) this._version = v;
    return v;
  }

  async isUp() {
    return !!(await this.version());
  }

  /**
   * Attach to the running Chrome or start one. Never restarts a live browser
   * (another process may be driving it).
   * @returns {{launched:boolean, version:object}}
   */
  async ensure({ launch = true } = {}) {
    let v = await this.version({ timeoutMs: 1200 });
    if (v) return { launched: false, version: v };
    if (!launch) throw new CliError(codes.BROWSER_DOWN, `no Chrome on 127.0.0.1:${this.port}`, { port: this.port, hint: 'run `alipay auth login` or start Chrome with the alipay-cli profile' });
    await this.launch();
    v = await this.version({ timeoutMs: 3000 });
    return { launched: true, version: v };
  }

  async launch() {
    const L = ensureLayout(this.home);
    if (!fileExists(this.chrome)) {
      throw new CliError(codes.CHROME_MISSING, `Chrome binary not found: ${this.chrome}`, { chrome: this.chrome, hint: 'set ALIPAY_CLI_CHROME' });
    }
    const args = [
      `--user-data-dir=${L.profile}`,
      `--remote-debugging-port=${this.port}`,
      '--no-first-run',
      '--no-default-browser-check',
      '--window-position=-32000,-32000',
      '--window-size=1280,900',
    ];
    this.log(`launching Chrome (port ${this.port}, profile ${L.profile})`);
    const child = spawn(this.chrome, args, { detached: true, stdio: 'ignore' });
    child.unref();
    const deadline = Date.now() + 25000;
    while (Date.now() < deadline) {
      await sleep(250);
      const v = await discover(this.port, { timeoutMs: 800 });
      if (v) {
        this._version = v;
        this._launched = true;
        return v;
      }
      if (child.exitCode != null) {
        throw new CliError(codes.BROWSER_DOWN, `Chrome exited during startup (code ${child.exitCode})`, { chrome: this.chrome });
      }
    }
    throw new CliError(codes.BROWSER_DOWN, `Chrome did not expose CDP on port ${this.port} within 25s`, {
      port: this.port,
      hint: 'another app may hold the port; set ALIPAY_CLI_CDP_PORT',
    });
  }

  async browserConn() {
    if (this._browserConn && !this._browserConn.closed) return this._browserConn;
    const v = await this.ensure({ launch: false });
    const conn = await CDP.connect(v.version.webSocketDebuggerUrl);
    this._browserConn = conn;
    return conn;
  }

  // Decisive fallback: chrome://version renders the profile directory that the
  // running browser actually uses. The probe tab is created and closed again.
  async verifyOwnershipByChromeVersion() {
    const L = layout(this.home);
    let page = null;
    try {
      page = await this.openPage({ url: 'chrome://version' });
      let text = '';
      let actual = null;
      // The row label can render before its value: wait for a path-shaped value,
      // otherwise the next label ("Linker") would be mistaken for the profile path.
      for (let i = 0; i < 40; i++) {
        await sleep(100);
        text = await page.bodyText();
        const m = /Profile Path\s+(\S+)/.exec(text);
        if (m && (m[1].startsWith('/') || /^[A-Za-z]:[\\/]/.test(m[1]))) {
          actual = m[1];
          break;
        }
      }
      if (!actual) return this.verifyOwnershipByPortFile();
      const expected = path.resolve(L.profile);
      if (path.resolve(actual) === expected || path.resolve(actual).startsWith(expected + path.sep)) {
        return { verified: true, method: 'chrome://version', profilePath: actual };
      }
      throw new CliError(codes.BROWSER_DOWN, 'the Chrome on this port is not the alipay-cli profile', {
        port: this.port,
        expected,
        actual,
        hint: 'refusing to touch a browser profile we do not own; stop that Chrome or change --port',
      });
    } catch (err) {
      if (err instanceof CliError) throw err;
      return this.verifyOwnershipByPortFile();
    } finally {
      if (page) {
        if (page.created) {
          const conn = this._browserConn;
          if (conn && !conn.closed) await conn.send('Target.closeTarget', { targetId: page.targetId }, { timeoutMs: 5000 }).catch(() => {});
        }
        await page.close().catch(() => {});
        this.pages = this.pages.filter((p) => p !== page);
      }
    }
  }

  // Chrome >= 130 no longer exposes Browser.getBrowserCommandLine. The profile
  // directory still gets a DevToolsActivePort file written by whichever Chrome
  // instance uses it, so a matching port is proof enough that the browser on our
  // port is running our profile.
  verifyOwnershipByPortFile() {
    const L = layout(this.home);
    const portFile = path.join(L.profile, 'DevToolsActivePort');
    try {
      const lines = fs.readFileSync(portFile, 'utf8').split('\n');
      const port = Number(lines[0]);
      if (port === this.port) return { verified: true, method: 'devtools-active-port', portFile };
      return { verified: false, reason: 'DevToolsActivePort port does not match', expected: this.port, actual: port || null, portFile };
    } catch {
      return { verified: false, reason: 'no DevToolsActivePort in the profile; cannot prove ownership' };
    }
  }

  // Confirm the Chrome on our port is really using the alipay-cli profile.
  async verifyOwnership() {
    const L = layout(this.home);
    let args = null;
    try {
      const conn = await this.browserConn();
      const res = await conn.send('Browser.getBrowserCommandLine', {}, { timeoutMs: 3000 });
      args = res?.arguments || null;
    } catch {
      // Chrome >= 130 dropped Browser.getBrowserCommandLine.
      return this.verifyOwnershipByChromeVersion();
    }
    if (!args || !args.length) return this.verifyOwnershipByPortFile();
    const flag = args.find((a) => a.startsWith('--user-data-dir='));
    if (!flag) return this.verifyOwnershipByPortFile();
    const dir = flag.slice('--user-data-dir='.length);
    let matches = false;
    try {
      matches = fs.realpathSync(dir) === fs.realpathSync(L.profile);
    } catch {
      matches = path.resolve(dir) === path.resolve(L.profile);
    }
    if (!matches) {
      throw new CliError(codes.BROWSER_DOWN, 'the Chrome on this port is not the alipay-cli profile', {
        port: this.port,
        expected: L.profile,
        actual: dir,
        hint: 'refusing to touch a browser profile we do not own; stop that Chrome or change --port',
      });
    }
    return { verified: true, method: 'browser-command-line' };
  }

  async openPage({ url = 'about:blank', targetId = null, reuse = false } = {}) {
    const conn = await this.browserConn();
    let id = targetId;
    let created = false;
    if (!id && reuse) {
      const pages = await this.listPages();
      const existing = pages.find((t) => /consumeprod\.alipay\.com|auth\.alipay\.com/.test(t.url || ''));
      if (existing) id = existing.id;
    }
    if (!id) {
      const res = await conn.send('Target.createTarget', { url }, { timeoutMs: 15000 });
      id = res.targetId;
      created = true;
    }
    // Resolve the per-target websocket.
    const deadline = Date.now() + 8000;
    let target;
    while (Date.now() < deadline) {
      const list = await listTargets(this.port);
      target = list.find((t) => t.id === id);
      if (target && target.webSocketDebuggerUrl) break;
      await sleep(150);
    }
    if (!target || !target.webSocketDebuggerUrl) {
      throw new CliError(codes.BROWSER_DOWN, 'could not attach to the new browser tab', { targetId: id });
    }
    const pageConn = await CDP.connect(target.webSocketDebuggerUrl);
    const page = new PageHandle({ targetId: id, conn: pageConn, url: target.url, created });
    this.pages.push(page);
    return page;
  }

  async listPages() {
    const list = await listTargets(this.port);
    return list.filter((t) => t.type === 'page');
  }

  async cookies() {
    const conn = await this.browserConn();
    const res = await conn.send('Storage.getCookies', {}, { timeoutMs: 15000 });
    return (res?.cookies || []).map(toInjectableCookie).filter(Boolean);
  }

  async setCookies(cookies) {
    const conn = await this.browserConn();
    const list = cookies.map(toInjectableCookie).filter(Boolean);
    if (!list.length) return { attempted: 0, ok: true };
    const res = await conn.send('Storage.setCookies', { cookies: list }, { timeoutMs: 30000 });
    return { attempted: list.length, ok: true, result: res || null };
  }

  vaultPaths() {
    const L = layout(this.home);
    return { dir: L.vault, file: L.vaultCookies, legacy: L.legacyCookies };
  }

  async saveCookies({ note = null } = {}) {
    const cookies = await this.cookies();
    if (!cookies.length) throw new CliError(codes.NEED_LOGIN, 'no cookies in the browser; nothing to save');
    const L = ensureLayout(this.home);
    ensureDir(L.vault, 0o700);
    const payload = { version: 1, savedAt: nowIso(), host: os.hostname(), port: this.port, note, count: cookies.length, cookies };
    writeFileAtomic(L.vaultCookies, JSON.stringify(payload, null, 2) + '\n', 0o600);
    return { file: L.vaultCookies, count: cookies.length, savedAt: payload.savedAt };
  }

  // Load the richest available snapshot: an earlier partial export must never
  // shadow a complete one (the vault may hold cookies.json and cookies-all.json).
  loadCookiesFromVault() {
    const { file, legacy } = this.vaultPaths();
    let best = null;
    for (const p of [file, legacy]) {
      if (!fs.existsSync(p)) continue;
      let parsed;
      try {
        parsed = JSON.parse(fs.readFileSync(p, 'utf8'));
      } catch (err) {
        throw new CliError(codes.BAD_INPUT, `cookie snapshot is unreadable: ${p}`, { file: p });
      }
      const cookies = Array.isArray(parsed) ? parsed : parsed.cookies;
      if (!Array.isArray(cookies) || !cookies.length) continue;
      const mapped = cookies.map(sanitizeCookieForVault).filter(Boolean);
      let mtime = 0;
      try {
        mtime = fs.statSync(p).mtimeMs;
      } catch {
        /* ignore */
      }
      const candidate = { file: p, savedAt: parsed.savedAt || null, mtime, cookies: mapped };
      if (!best || mapped.length > best.cookies.length || (mapped.length === best.cookies.length && mtime > best.mtime)) best = candidate;
    }
    if (!best) return null;
    delete best.mtime;
    return best;
  }

  async injectVaultCookies() {
    const snap = this.loadCookiesFromVault();
    if (!snap) return { injected: 0, source: null };
    const r = await this.setCookies(snap.cookies);
    return { injected: r.attempted, source: snap.file, savedAt: snap.savedAt };
  }

  /**
   * Detach from Chrome. Tabs created by this process are closed again unless
   * `keepTabs` is set; the browser itself is never shut down (a login session
   * is meant to survive between CLI invocations).
   */
  async dispose({ keepTabs = false } = {}) {
    if (this._browserConn && !this._browserConn.closed) {
      for (const p of this.pages) {
        if (p.created && !keepTabs) {
          await this._browserConn.send('Target.closeTarget', { targetId: p.targetId }, { timeoutMs: 5000 }).catch(() => {});
        }
        await p.close().catch(() => {});
      }
      this.pages = [];
      this._browserConn.close();
      this._browserConn = null;
      return;
    }
    for (const p of this.pages) await p.close().catch(() => {});
    this.pages = [];
  }

  async close() {
    return this.dispose();
  }
}

module.exports = { BrowserSession, PageHandle, defaultPort, chromePath, toInjectableCookie, DEFAULT_CHROME, DEFAULT_PORT };
