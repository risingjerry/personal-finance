'use strict';
// `alipay auth login` / `alipay auth status`
const { BrowserSession } = require('../browser');
const session = require('../session');
const { CliError, codes } = require('../errors');
const { layout, safeUrl } = require('../util');

function browserSummary(browser, extra = {}) {
  const L = layout(browser.home);
  return { port: browser.port, profile: L.profile, vault: L.vaultCookies, ...extra };
}

async function login(ctx) {
  const timeoutMs = Math.max(30, Number(ctx.flags.timeout ?? 300)) * 1000;
  const browser = new BrowserSession({ home: ctx.home, port: ctx.port, log: ctx.log });
  try {
    const res = await session.login({ browser, log: ctx.log, timeoutMs });
    return {
      cmd: 'auth.login',
      data: {
        session: 'ok',
        cookies: { count: res.saved.count, savedAt: res.saved.savedAt, file: res.saved.file },
        browser: browserSummary(browser, { launched: res.launched }),
        checkedUrl: safeUrl(res.url),
      },
      meta: { elapsedMs: res.ms },
    };
  } finally {
    await browser.dispose();
  }
}

async function status(ctx) {
  const browser = new BrowserSession({ home: ctx.home, port: ctx.port, log: ctx.log });
  const launch = !!ctx.flags.launch;
  try {
    const up = await browser.isUp();
    let vault = null;
    try {
      const snap = browser.loadCookiesFromVault();
      vault = snap ? { file: snap.file, savedAt: snap.savedAt, count: snap.cookies.length } : null;
    } catch (err) {
      vault = { error: err.message };
    }
    if (!up && !launch) {
      return {
        cmd: 'auth.status',
        data: {
          session: 'browser_down',
          browser: browserSummary(browser, { up: false }),
          vault,
          hint: 'Chrome is not running on the alipay-cli port; run `alipay auth status --launch` (or `alipay sync`) to start it.',
        },
        meta: {},
      };
    }
    const ready = await session.ensureReady({ browser, log: ctx.log });
    return {
      cmd: 'auth.status',
      data: {
        session: ready.session,
        reason: ready.check.reason,
        url: safeUrl(ready.check.url),
        browser: browserSummary(browser, { up: true, launched: ready.launched }),
        vault: ready.vault,
        injectedFromVault: ready.injected,
        checkedAt: ready.check.checkedAt,
      },
      meta: { elapsedMs: ready.check.ms },
    };
  } finally {
    await browser.dispose();
  }
}

module.exports = { login, status, browserSummary };
