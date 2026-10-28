'use strict';
// Login state + interactive login flow.
const { CliError, codes } = require('./errors');
const { sleep, nowIso, safeUrl } = require('./util');
const { ORDERS_PAGE, captureTemplate } = require('./api');

const LOGIN_URL = 'https://www.okx.com/zh-hans/account/login';

function classify(url = '', text = '') {
  if (/\/account\/login/.test(url)) return 'need_login';
  if (/verify|security|challenge|captcha/i.test(url) && !/p2p/.test(url)) return 'security_challenge';
  if (/p2p|account/.test(url)) return 'ok';
  if (/请登录|登录后|扫码登录|Log in/i.test(text) && text.length < 400) return 'need_login';
  return 'unknown';
}

async function checkSession(page, { timeoutMs = 30000 } = {}) {
  const t0 = Date.now();
  await page.navigate(ORDERS_PAGE, { timeoutMs });
  const deadline = Math.min(Date.now() + Math.min(timeoutMs, 15000), t0 + timeoutMs);
  let last = { url: '', text: '' };
  while (Date.now() < deadline) {
    try {
      last = { url: await page.url(), text: await page.bodyText() };
    } catch {
      await sleep(200);
      continue;
    }
    const cls = classify(last.url, last.text);
    if (cls !== 'unknown') {
      return { session: cls, url: last.url, excerpt: String(last.text || '').slice(0, 160), checkedAt: nowIso(), ms: Date.now() - t0 };
    }
    await sleep(400);
  }
  return { session: classify(last.url, last.text), url: last.url, excerpt: String(last.text || '').slice(0, 160), checkedAt: nowIso(), ms: Date.now() - t0 };
}

/** Make sure we have a logged-in browser and a usable request template. */
async function ensureReady({ browser, log = () => {}, refreshTemplate = false, timeoutMs = 30000 } = {}) {
  const { launched } = await browser.ensure({ launch: true });
  await browser.verifyOwnership();
  const page = await browser.openPage({ url: ORDERS_PAGE });
  const check = await checkSession(page, { timeoutMs });
  if (check.session === 'need_login') {
    throw new CliError(codes.NEED_LOGIN, 'OKX session expired: run `okx auth login` and log in in the opened window', {
      url: safeUrl(check.url),
      hint: 'okx auth login',
    });
  }
  const file = browser.layout().template;  // per --home: never another account's cached headers
  const template = refreshTemplate ? await captureTemplate(page, { log }) : await loadOrCaptureTemplate(page, { log, file });
  return { page, check, template, launched };
}

async function loadOrCaptureTemplate(page, { log, file } = {}) {
  const { readJson, writeFileAtomic } = require('./util');
  const cached = readJson(file, null);
  const fresh = cached && cached.capturedAt && Date.now() - Date.parse(cached.capturedAt) < 20 * 60 * 1000;
  if (fresh && cached.headers && cached.headers.Authorization) return cached;
  const tpl = await captureTemplate(page, { log });
  writeFileAtomic(file, JSON.stringify(tpl, null, 1), 0o600);
  return tpl;
}

/** Interactive login: open the login page in the visible window and wait. */
async function login({ browser, log = () => {}, timeoutMs = 300000 } = {}) {
  const t0 = Date.now();
  await browser.ensure({ launch: true });
  const page = await browser.openPage({ url: LOGIN_URL });
  await page.send('Page.bringToFront').catch(() => {});
  log('login window ready: scan the QR code or sign in; waiting…');
  const deadline = Date.now() + timeoutMs;
  let lastUrl = '';
  while (Date.now() < deadline) {
    await sleep(1500);
    try {
      lastUrl = await page.url();
    } catch { continue; }
    if (!/\/account\/login/.test(lastUrl) && /okx\.com/.test(lastUrl)) {
      // Give the SPA a moment to settle, then confirm against the orders page.
      await sleep(1500);
      const check = await checkSession(page, { timeoutMs: 20000 });
      if (check.session === 'ok') {
        // Capture a fresh request template right away; the session is hot now.
        const tpl = await captureTemplate(page, { log }).catch(() => null);
        if (tpl) {
          const { writeFileAtomic } = require('./util');
          writeFileAtomic(browser.layout().template, JSON.stringify(tpl, null, 1), 0o600);
        }
        return { session: 'ok', url: safeUrl(check.url), templateCaptured: !!tpl, ms: Date.now() - t0 };
      }
      if (check.session === 'need_login') { lastUrl = check.url; continue; }
    }
  }
  throw new CliError(codes.NEED_LOGIN, 'login did not complete in time', { lastUrl: safeUrl(lastUrl), timeoutMs });
}

module.exports = { login, ensureReady, checkSession, classify, LOGIN_URL };
