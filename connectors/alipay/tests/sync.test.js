'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { runSync, planYears, yearWindow, chunk } = require('../src/sync');
const { readMeta, loadBills } = require('../src/store');
const { CliError, codes } = require('../src/errors');
const { buildZip, statementCsv, yearRows } = require('./helpers/fixtures');

function tempHome() {
  return fs.mkdtempSync(path.join(os.tmpdir(), 'alipay-sync-'));
}

function zipFor(begin, end) {
  const y0 = Number(begin.slice(0, 4));
  const y1 = /-01-01$/.test(end) ? Number(end.slice(0, 4)) - 1 : Number(end.slice(0, 4));
  const rows = [];
  for (let y = y0; y <= y1; y++) rows.push(...yearRows(y, 2));
  return buildZip({ [`alipay_record_${y0}.csv`]: Buffer.from(statementCsv({ begin, end, rows }), 'utf8') });
}

function harness(overrides = {}) {
  const home = tempHome();
  const calls = [];
  const exporter = async ({ begin, end }) => {
    calls.push({ begin, end });
    const result = overrides.respond ? await overrides.respond({ begin, end, call: calls.length }) : null;
    if (result) return result;
    return { ok: true, buffer: zipFor(begin, end), fileName: `alipay_record_${begin}.zip` };
  };
  return {
    home,
    calls,
    run: (opts = {}) =>
      runSync({
        home,
        browser: null,
        ensureSession: async () => ({ page: {}, session: 'ok' }),
        exporter,
        ensureRecordView: async () => ({ ok: true }),
        sleepFn: async () => {},
        backoffSeconds: [0],
        log: () => {},
        ...opts,
      }),
  };
}

test('window boundaries: closed years end on the next Jan 1, current year ends tomorrow', () => {
  const past = yearWindow(2024, { today: '2026-10-26' });
  assert.deepEqual(past, { begin: '2024-01-01', end: '2025-01-01' });
  const current = yearWindow(2026, { today: '2026-10-26' });
  assert.deepEqual(current, { begin: '2026-01-01', end: '2026-10-27' });
});

test('planYears skips completed years unless forced', () => {
  const meta = { years: { 2024: { status: 'complete' }, 2025: { status: 'empty' } } };
  const p = planYears({ from: 2024, to: 2026, meta, force: false });
  assert.deepEqual(p.planned, [2026]);
  assert.equal(p.skipped.length, 2);
  const forced = planYears({ from: 2024, to: 2026, meta, force: true });
  assert.deepEqual(forced.planned, [2024, 2025, 2026]);
  assert.throws(() => planYears({ from: 2026, to: 2024, meta, force: false }));
});

test('chunk groups years for multi-year exports', () => {
  assert.deepEqual(chunk([2024, 2025, 2026], 2), [[2024, 2025], [2026]]);
  assert.deepEqual(chunk([2024], 1), [[2024]]);
});

test('sync writes raw evidence, records and per-year meta', async () => {
  const h = harness();
  const res = await h.run({ from: 2024, to: 2025, throttleSeconds: 0 });
  assert.equal(res.ok, true);
  assert.equal(res.years.length, 2);
  assert.equal(res.added, 4);
  assert.equal(res.records, 4);
  assert.equal(h.calls.length, 2);

  const { records } = loadBills(h.home);
  assert.equal(records.length, 4);
  assert.deepEqual(records.map((r) => r.year).sort(), [2024, 2024, 2025, 2025]);

  const meta = readMeta(h.home);
  assert.equal(meta.years['2024'].status, 'complete');
  assert.equal(meta.years['2025'].status, 'complete');
  assert.equal(meta.years['2024'].rows, 2);
  assert.equal(meta.records, 4);

  const raw = fs.readdirSync(path.join(h.home, 'data', 'raw'));
  assert.equal(raw.filter((f) => f.endsWith('.zip')).length, 2);
  assert.equal(raw.filter((f) => f.endsWith('.csv')).length, 2);
});

test('a second run is a no-op because every year is already complete', async () => {
  const h = harness();
  await h.run({ from: 2024, to: 2024, throttleSeconds: 0 });
  const callsBefore = h.calls.length;
  const again = await h.run({ from: 2024, to: 2024, throttleSeconds: 0 });
  assert.equal(h.calls.length, callsBefore);
  assert.equal(again.years.length, 0);
  assert.equal(again.skipped.length, 1);
});

test('rate limiting backs off, then completes the pending year', async () => {
  const h = harness({
    respond: async ({ call }) => (call === 1 ? { ok: false, code: codes.RATE_LIMITED, detail: { state: 'rate_limited' } } : null),
  });
  const res = await h.run({ from: 2024, to: 2024, throttleSeconds: 0, maxRetries: 2, adaptive: false });
  assert.equal(res.years.length, 1);
  assert.equal(res.rateLimited, true);
  assert.equal(h.calls.length, 2, 'one retry after the backoff');
  assert.equal(readMeta(h.home).years['2024'].status, 'complete');
});

test('rate limiting that persists leaves the year pending and keeps earlier work', async () => {
  const h = harness({
    respond: async ({ begin }) => (begin.startsWith('2025') ? { ok: false, code: codes.RATE_LIMITED, detail: {} } : null),
  });
  const res = await h.run({ from: 2024, to: 2025, throttleSeconds: 0, maxRetries: 1, adaptive: false });
  assert.equal(res.years.length, 1, '2024 completed');
  assert.equal(res.pending.length, 1);
  assert.equal(res.pending[0].year, '2025');
  assert.equal(readMeta(h.home).years['2024'].status, 'complete');
  assert.equal(readMeta(h.home).years['2025'], undefined);
  assert.ok(readMeta(h.home).rateLimit);
});

test('need_login aborts the whole run and reports completed work', async () => {
  const h = harness({
    respond: async ({ begin }) => (begin.startsWith('2025') ? { ok: false, code: codes.NEED_LOGIN, detail: { state: 'need_login' } } : null),
  });
  await assert.rejects(
    () => h.run({ from: 2024, to: 2025, throttleSeconds: 0 }),
    (err) => err instanceof CliError && err.code === codes.NEED_LOGIN && err.details.completed.length === 1,
  );
  assert.equal(loadBills(h.home).records.length, 2, 'year 2024 stayed committed');
});

test('a bad zip is reported but does not abort the other years', async () => {
  const h = harness({
    respond: async ({ begin }) => (begin.startsWith('2024') ? { ok: true, buffer: Buffer.from('PK\x03\x04garbage'), fileName: 'x.zip' } : null),
  });
  const res = await h.run({ from: 2024, to: 2025, throttleSeconds: 0 });
  assert.equal(res.years.length, 1);
  assert.equal(res.years[0].years[0], 2025);
  assert.equal(res.failed.length, 1);
  assert.equal(res.ok, false);
  assert.equal(readMeta(h.home).years['2024'], undefined);
});

test('span-years exports one window for several years and marks each year', async () => {
  const h = harness();
  const res = await h.run({ from: 2024, to: 2025, throttleSeconds: 0, spanYears: 2 });
  assert.equal(h.calls.length, 1);
  assert.deepEqual(h.calls[0], { begin: '2024-01-01', end: '2026-01-01' });
  assert.equal(res.years[0].years.length, 2);
  assert.equal(res.records, 4);
  const meta = readMeta(h.home);
  assert.equal(meta.years['2024'].status, 'complete');
  assert.equal(meta.years['2025'].status, 'complete');
  assert.equal(meta.years['2025'].span, 2);
});

test('an empty statement marks the year empty instead of retrying forever', async () => {
  const h = harness({
    respond: async ({ begin, end }) => ({
      ok: true,
      buffer: buildZip({ 'empty.csv': Buffer.from(statementCsv({ begin, end, rows: [] }), 'utf8') }),
      fileName: 'empty.zip',
    }),
  });
  const res = await h.run({ from: 2016, to: 2016, throttleSeconds: 0 });
  assert.equal(res.ok, true);
  assert.equal(readMeta(h.home).years['2016'].status, 'empty');
  assert.equal(readMeta(h.home).years['2016'].rows, 0);
});


test('a rate-limited year is split into smaller windows instead of backing off', async () => {
  const h = harness({
    respond: async ({ begin, end }) => {
      const days = (Date.parse(end) - Date.parse(begin)) / 86400000;
      return days > 200 ? { ok: false, code: codes.RATE_LIMITED, detail: { state: 'rate_limited' } } : null;
    },
  });
  const res = await h.run({ from: 2024, to: 2024, throttleSeconds: 0, maxRetries: 0 });
  assert.equal(h.calls.length, 3, 'one rejected year + two smaller windows');
  assert.ok(h.calls[1].end === h.calls[2].begin, 'split windows must not overlap or leave a gap');
  assert.equal(h.calls[1].begin, '2024-01-01');
  assert.equal(h.calls[2].end, '2025-01-01');
  assert.equal(res.years.length, 2);
  assert.equal(res.pending.length, 0);
  // Two halves cover the whole year: the year is complete and both parts are kept
  // so a later resume never re-exports them.
  const meta = readMeta(h.home).years['2024'];
  assert.equal(meta.status, 'complete');
  assert.equal(meta.partial, false);
  assert.deepEqual(meta.parts.map((p) => [p.begin, p.end]), [['2024-01-01', '2025-01-01']]);
});
