'use strict';
// `alipay sync` — export year windows from Alipay into the local store.
const { BrowserSession } = require('../browser');
const session = require('../session');
const { runSync } = require('../sync');
const { acquireLock } = require('../lock');
const { updateMeta } = require('../store');
const { CliError, codes, usage } = require('../errors');
const { nowIso, safeUrl } = require('../util');

function intFlag(flags, name, fallback, { min = 0, max = 100 } = {}) {
  if (flags[name] === undefined) return fallback;
  const n = Number(flags[name]);
  if (!Number.isInteger(n) || n < min || n > max) throw usage(`--${name} must be an integer between ${min} and ${max}`, { got: flags[name] });
  return n;
}

async function run(ctx) {
  const flags = ctx.flags;
  const from = flags.from === undefined ? undefined : intFlag(flags, 'from', undefined, { min: 2004, max: 2100 });
  const to = flags.to === undefined ? undefined : intFlag(flags, 'to', undefined, { min: 2004, max: 2100 });
  const spanYears = intFlag(flags, 'span-years', 1, { min: 1, max: 5 });
  const throttleSeconds = flags.throttle === undefined ? undefined : intFlag(flags, 'throttle', undefined, { min: 0, max: 3600 });
  const maxRetries = intFlag(flags, 'max-retries', 2, { min: 0, max: 10 });

  const lock = acquireLock(ctx.home, { cmd: 'sync' });
  const browser = new BrowserSession({ home: ctx.home, port: ctx.port, log: ctx.log });
  try {
    const res = await runSync({
      home: ctx.home,
      from,
      to,
      force: !!flags.force,
      spanYears,
      throttleSeconds,
      maxRetries,
      log: ctx.log,
      browser,
      ensureSession: async ({ browser: b }) => {
        const ready = await session.ensureReady({ browser: b, log: ctx.log });
        if (ready.session !== 'ok') {
          const code = ready.session === 'security_challenge' ? codes.SECURITY_CHALLENGE : codes.NEED_LOGIN;
          throw new CliError(code, `sync aborted: session is ${ready.session}`, {
            session: ready.session,
            url: safeUrl(ready.check.url),
            injectedFromVault: ready.injected,
            hint: ready.session === 'need_login' ? 'run `alipay auth login`' : 'complete the security verification in the Chrome window, then retry',
          });
        }
        return ready;
      },
    });

    if (res.years.length) updateMeta(ctx.home, { lastSyncAt: nowIso() });
    const status = res.pending.length || res.failed.length ? (res.years.length ? 'partial' : res.rateLimited ? 'rate_limited' : 'failed') : res.years.length ? 'complete' : 'nothing_to_do';
    return {
      cmd: 'sync',
      data: { ...res, status, ok: status === 'complete' || status === 'nothing_to_do' },
      meta: {
        home: ctx.home,
        port: ctx.port,
        from: from ?? null,
        to: to ?? null,
        spanYears,
        throttleSeconds: throttleSeconds ?? require('../sync').DEFAULT_THROTTLE,
        maxRetries,
        force: !!flags.force,
      },
      exitCode: status === 'complete' || status === 'nothing_to_do' ? 0 : 1,
    };
  } finally {
    await browser.dispose();
    lock.release();
  }
}

module.exports = { run };
