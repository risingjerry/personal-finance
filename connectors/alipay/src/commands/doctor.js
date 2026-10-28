'use strict';
// `alipay doctor` — environment and data self-check. Never prints secrets.
const fs = require('node:fs');
const path = require('node:path');
const { BrowserSession, chromePath } = require('../browser');
const session = require('../session');
const { loadBills, readMeta } = require('../store');
const { lockStatus } = require('../lock');
const { layout, fileExists, statSafe, humanBytes, nowIso, safeUrl } = require('../util');
const keepalive = require('../keepalive');

function check(results, name, status, detail) {
  results.push({ name, status, detail });
}

function nodeChecks(results) {
  const major = Number(process.versions.node.split('.')[0]);
  check(results, 'node', major >= 22 ? 'pass' : 'fail', { version: process.versions.node, required: '>=22' });
  check(results, 'websocket', typeof WebSocket === 'function' ? 'pass' : 'fail', { global: typeof WebSocket === 'function' });
  let gb = false;
  try {
    gb = new TextDecoder('gb18030').decode(Buffer.from([0xd5, 0xcb])) === '账';
  } catch {
    gb = false;
  }
  check(results, 'gb18030-decoder', gb ? 'pass' : 'fail', { decodedOk: gb });
}

function fsChecks(results, home) {
  const L = layout(home);
  check(results, 'chrome-binary', fileExists(chromePath()) ? 'pass' : 'fail', { path: chromePath() });
  const homeStat = statSafe(L.home);
  check(results, 'home-dir', homeStat ? 'pass' : 'warn', { path: L.home, exists: !!homeStat, mode: homeStat ? (homeStat.mode & 0o777).toString(8) : null });
  const profileStat = statSafe(L.profile);
  check(results, 'chrome-profile', profileStat ? 'pass' : 'warn', { path: L.profile, exists: !!profileStat, hint: profileStat ? undefined : 'created on first launch' });
  const vaultStat = statSafe(L.vault);
  const cookieStat = statSafe(L.vaultCookies);
  check(results, 'cookie-vault', cookieStat ? 'pass' : 'warn', {
    dir: L.vault,
    dirMode: vaultStat ? (vaultStat.mode & 0o777).toString(8) : null,
    file: L.vaultCookies,
    fileMode: cookieStat ? (cookieStat.mode & 0o777).toString(8) : null,
    bytes: cookieStat ? cookieStat.size : null,
    hint: cookieStat ? undefined : 'run `alipay auth login` once',
  });
}

function dataChecks(results, home) {
  const L = layout(home);
  const billsStat = statSafe(L.bills);
  const { records, badLines } = loadBills(home);
  const meta = readMeta(home);
  check(results, 'bills-store', billsStat ? (badLines ? 'warn' : 'pass') : 'warn', {
    file: L.bills,
    exists: !!billsStat,
    bytes: billsStat ? billsStat.size : null,
    humanSize: billsStat ? humanBytes(billsStat.size) : null,
    records: records.length,
    corruptLines: badLines,
  });

  // Integrity: duplicate keys and unusable amounts.
  const seen = new Set();
  let dup = 0;
  let badAmount = 0;
  let badDate = 0;
  for (const r of records) {
    if (seen.has(r.trade_no)) dup++;
    seen.add(r.trade_no);
    if (r.amount == null) badAmount++;
    if (r.created_at && Number.isNaN(Date.parse(r.created_at))) badDate++;
  }
  check(results, 'bills-integrity', dup || badAmount || badDate ? 'warn' : records.length ? 'pass' : 'warn', {
    records: records.length,
    duplicateTradeNo: dup,
    invalidAmounts: badAmount,
    invalidDates: badDate,
  });

  const years = Object.entries(meta.years || {})
    .map(([y, v]) => ({ year: Number(y), status: v.status, rows: v.rows ?? null, exportedAt: v.exportedAt ?? null }))
    .sort((a, b) => a.year - b.year);
  const complete = years.filter((y) => y.status === 'complete' || y.status === 'empty').length;
  check(results, 'sync-state', years.length ? 'pass' : 'warn', {
    yearsKnown: years.length,
    yearsComplete: complete,
    years,
    lastSyncAt: meta.lastSyncAt || null,
    lastAttemptAt: meta.lastAttemptAt || null,
    rateLimit: meta.rateLimit || null,
    records: meta.records ?? records.length,
  });

  const rawDir = L.raw;
  let rawFiles = [];
  try {
    rawFiles = fs.readdirSync(rawDir).filter((f) => /\.(zip|csv)$/i.test(f));
  } catch {
    /* missing dir is fine */
  }
  const zips = rawFiles.filter((f) => f.endsWith('.zip'));
  check(results, 'raw-evidence', zips.length ? 'pass' : 'warn', {
    dir: rawDir,
    zips: zips.length,
    csvs: rawFiles.length - zips.length,
    sample: zips.slice(-3),
    hint: zips.length ? undefined : 'no raw exports yet (import or sync to create some)',
  });

  try {
    const ka = keepalive.status({ home });
    const kaStatus = !ka.installed ? 'skip' : ka.loaded ? 'pass' : 'warn';
    check(results, 'keepalive', kaStatus, {
      installed: ka.installed,
      loaded: ka.loaded,
      intervalSeconds: ka.intervalSeconds,
      lastRun: ka.lastRun,
      lastSession: ka.lastSession,
      optional: true,
      hint: ka.installed ? undefined : 'optional: `alipay keepalive install` keeps an idle session alive; the default workflow scans per sync',
    });
  } catch (err) {
    check(results, 'keepalive', 'warn', { message: err.message });
  }

  const lock = lockStatus(home);
  check(results, 'lock', lock.locked ? 'warn' : 'pass', { path: lock.path, locked: lock.locked, pid: lock.pid ?? null, startedAt: lock.startedAt ?? null });
}

async function browserChecks(results, home, port, { online = true, log = () => {} } = {}) {
  const browser = new BrowserSession({ home, port, log });
  try {
    const up = await browser.isUp();
    check(results, 'cdp', up ? 'pass' : 'warn', { port, up, hint: up ? undefined : 'Chrome not running; `alipay auth login` starts it' });
    if (!up) {
      check(results, 'session', 'skip', { reason: 'browser not running' });
      return;
    }
    let owned = true;
    try {
      const own = await browser.verifyOwnership();
      check(results, 'profile-ownership', own.verified ? 'pass' : 'warn', own);
      owned = own.verified !== false;
    } catch (err) {
      // Not fatal for doctor: the port may belong to another browser, in which
      // case we must not touch it (and never inject cookies into it).
      check(results, 'profile-ownership', 'warn', { message: err.message, ...(err.details || {}) });
      owned = false;
    }
    if (!online) {
      check(results, 'session', 'skip', { reason: '--offline' });
      return;
    }
    if (!owned) {
      check(results, 'session', 'skip', { reason: 'profile ownership could not be verified; refusing to use this browser' });
      return;
    }
    const ready = await session.ensureReady({ browser, log, injectIfNeeded: true });
    check(results, 'session', ready.session === 'ok' ? 'pass' : ready.session === 'security_challenge' ? 'warn' : 'fail', {
      session: ready.session,
      reason: ready.check.reason,
      url: safeUrl(ready.check.url),
      injectedFromVault: ready.injected,
      vault: ready.vault,
      ms: ready.check.ms,
    });
  } finally {
    await browser.dispose();
  }
}

async function run(ctx) {
  const results = [];
  nodeChecks(results);
  fsChecks(results, ctx.home);
  dataChecks(results, ctx.home);
  await browserChecks(results, ctx.home, ctx.port, { online: !ctx.flags.offline, log: ctx.log });

  const summary = {
    pass: results.filter((r) => r.status === 'pass').length,
    warn: results.filter((r) => r.status === 'warn').length,
    fail: results.filter((r) => r.status === 'fail').length,
    skip: results.filter((r) => r.status === 'skip').length,
  };
  return {
    cmd: 'doctor',
    data: { ok: summary.fail === 0, summary, checks: results, checkedAt: nowIso() },
    meta: { home: ctx.home, port: ctx.port },
    exitCode: summary.fail === 0 ? 0 : 1,
  };
}

module.exports = { run };
