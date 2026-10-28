'use strict';
// Session orchestration: is the browser logged in, do we need to inject the
// vault snapshot, and the interactive QR-login flow.
const { CliError, codes } = require('./errors');
const alipay = require('./alipay');
const { sleep, nowIso, safeUrl } = require('./util');



/**
 * Open the personal record list and classify the session.
 * @returns {{session:'ok'|'need_login'|'security_challenge'|'unknown', url:string,
 *            excerpt:string, checkedAt:string, ms:number}}
 */
/** Refuse to put vault cookies into a Chrome we cannot prove is the alipay-cli profile. */
async function requireOwnBrowser(browser) {
  const own = await browser.verifyOwnership();
  if (own && own.verified === false) {
    throw new CliError(codes.BROWSER_DOWN, 'cannot prove the Chrome on this port is the alipay-cli profile', {
      reason: own.reason, hint: 'close that Chrome or use --port with a free port',
    });
  }
}

async function checkSession({ page, timeoutMs = 30000 } = {}) {
  const t0 = Date.now();
  await page.navigate(alipay.STANDARD_PAGE, { timeoutMs });
  const deadline = Date.now() + Math.min(timeoutMs, 20000);
  let last = { url: '', text: '' };
  while (Date.now() < deadline) {
    try {
      last = { url: await page.currentUrl(), text: await page.bodyText() };
    } catch {
      await sleep(200);
      continue;
    }
    const cls = alipay.classifySession(last);
    if (cls.session !== 'unknown') {
      // Read once more after a short settle so we never classify a half-rendered
      // document (an empty <body> briefly matches no markers at all).
      await sleep(250);
      try {
        last = { url: await page.currentUrl(), text: await page.bodyText() };
      } catch {
        /* keep previous sample */
      }
      const final = alipay.classifySession(last);
      return {
        session: final.session,
        reason: final.reason,
        url: last.url,
        excerpt: String(last.text || '').slice(0, 200),
        checkedAt: nowIso(),
        ms: Date.now() - t0,
      };
    }
    await sleep(400);
  }
  const final = alipay.classifySession(last);
  return {
    session: final.session,
    reason: final.reason || 'timed out waiting for page markers',
    url: last.url,
    excerpt: String(last.text || '').slice(0, 200),
    checkedAt: nowIso(),
    ms: Date.now() - t0,
  };
}

/**
 * Make sure the browser can reach the personal records without a login:
 * 1. reuse a running browser / launch ours,
 * 2. verify the profile is ours,
 * 3. check the session; on a fresh process with a vault snapshot, inject the
 *    snapshot and re-check (session cookies die with Chrome).
 * @returns {{page:object, session:string, injected:boolean, launched:boolean,
 *            check:object, vault:object|null}}
 */
async function ensureReady({ browser, log = () => {}, injectIfNeeded = true, timeoutMs = 30000 } = {}) {
  const { launched } = await browser.ensure({ launch: true });
  await requireOwnBrowser(browser);
  const page = await browser.openPage({ url: alipay.STANDARD_PAGE });
  let check = await checkSession({ page, timeoutMs });
  let injected = false;

  if (check.session !== 'ok' && injectIfNeeded) {
    const snap = browser.loadCookiesFromVault();
    if (snap) {
      log(`session ${check.session}; injecting ${snap.cookies.length} cookies from vault (saved ${snap.savedAt || 'unknown'})`);
      await browser.injectVaultCookies();
      injected = true;
      check = await checkSession({ page, timeoutMs });
    }
  }

  // Keep the vault warm: Alipay can rotate session cookies, and the next cold
  // start has to inject what the live browser holds now, not a stale copy.
  if (check.session === 'ok') {
    try {
      await browser.saveCookies({ note: injected ? 'post-injection refresh' : 'auto-refresh' });
    } catch {
      /* non-fatal: the live session still works */
    }
  }

  const vault = browser.loadCookiesFromVault();
  return {
    page,
    session: check.session,
    injected,
    launched,
    check,
    vault: vault ? { file: vault.file, savedAt: vault.savedAt, count: vault.cookies.length } : null,
  };
}

/**
 * Interactive QR login: open auth.alipay.com and wait for the session to land.
 * Saves a cookie snapshot on success.
 */
async function login({ browser, log = () => {}, timeoutMs = 300000 } = {}) {
  const { launched } = await browser.ensure({ launch: true });
  await requireOwnBrowser(browser);
  const page = await browser.openPage({ url: alipay.LOGIN_PAGE });
  await page.navigate(alipay.LOGIN_PAGE, { timeoutMs: 60000 });
  log('scan the QR code with the Alipay app (window is positioned off-screen)');

  const deadline = Date.now() + timeoutMs;
  let lastUrl = '';
  let loginSeenAt = null;
  while (Date.now() < deadline) {
    let url = '';
    let text = '';
    try {
      url = await page.currentUrl();
      text = await page.bodyText();
    } catch {
      await sleep(500);
      continue;
    }
    lastUrl = url;
    if (!url.includes('auth.alipay.com')) loginSeenAt = nowIso();
    const cls = alipay.classifySession({ url, text });
    if (cls.session === 'ok') {
      log('login detected on the record page');
      const saved = await browser.saveCookies({ note: 'auth login' });
      return { ok: true, session: 'ok', saved, launched, url, ms: Date.now() - (deadline - timeoutMs) };
    }
    // The landing page after scanning is the merchant console; treat a
    // realized session there as success and confirm on standard.htm.
    if (!url.includes('auth.alipay.com') && /退出|我的账单|账户|首页/.test(text)) {
      log('alipay.com session detected, verifying against the record page');
      const check = await checkSession({ page, timeoutMs: 20000 });
      if (check.session === 'ok') {
        const saved = await browser.saveCookies({ note: 'auth login' });
        return { ok: true, session: 'ok', saved, launched, url: check.url, ms: Date.now() - (deadline - timeoutMs) };
      }
      if (check.session === 'security_challenge') {
        log('security verification is pending in the browser window; waiting');
        loginSeenAt = loginSeenAt || nowIso();
      }
    }
    await sleep(2000);
  }
  throw new CliError(codes.NEED_LOGIN, `login not completed within ${Math.round(timeoutMs / 1000)}s`, {
    lastUrl: safeUrl(lastUrl),
    hint: 'run `alipay auth login` again and scan promptly',
    loginPageSeen: loginSeenAt,
  });
}

module.exports = { checkSession, ensureReady, login };
