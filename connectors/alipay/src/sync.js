'use strict';
// Sync engine: one export per year window (or span of years), throttled,
// resumable, rate-limit aware.
const path = require('node:path');
const { CliError, codes } = require('./errors');
const { readZipEntries, pickCsvEntry } = require('./zip');
const { parseStatement, decodeAuto } = require('./csv');
const { commitRecords, readMeta, writeMeta } = require('./store');
const { ensureLayout, ensureDir, writeFileAtomic, sha256, nowIso, shanghaiToday, addDays, stampCompact, sleep, safeUrl } = require('./util');
const alipay = require('./alipay');

const BACKOFF_SECONDS = [60, 120, 300];
const DEFAULT_THROTTLE = 45;
const MIN_DATA_YEAR = 2017;

function currentShanghaiYear(today = shanghaiToday()) {
  return Number(today.slice(0, 4));
}

/**
 * Export window for a year span. End dates are exclusive: Alipay's endDate
 * behaves as 00:00 of that day; records on the end date are excluded.
 */
function yearWindow(year, { today = shanghaiToday() } = {}) {
  const begin = `${year}-01-01`;
  const end = year >= currentShanghaiYear(today) ? addDays(today, 1) : `${year + 1}-01-01`;
  return { begin, end };
}

function chunk(list, size) {
  const sizeN = Math.max(1, Number(size) || 1);
  const out = [];
  for (let i = 0; i < list.length; i += sizeN) out.push(list.slice(i, i + sizeN));
  return out;
}

function planYears({ from, to, meta, force }) {
  const maxYear = to ?? currentShanghaiYear();
  const minYear = from ?? MIN_DATA_YEAR;
  if (minYear > maxYear) throw new CliError(codes.BAD_INPUT, `--from ${minYear} is after --to ${maxYear}`);
  const planned = [];
  const skipped = [];
  for (let y = minYear; y <= maxYear; y++) {
    const state = meta.years?.[String(y)];
    const done = state && (state.status === 'complete' || state.status === 'empty');
    if (done && !force) {
      skipped.push({ year: y, reason: state.status, rows: state.rows ?? null, exportedAt: state.exportedAt ?? null });
      continue;
    }
    planned.push(y);
  }
  return { planned, skipped };
}

function validateStatement(parsed, { begin, end, years }) {
  const issues = [];
  if (!parsed.records.length) return { issues, empty: true };
  const allowed = new Set(years.map(String));
  const outside = parsed.records.filter((r) => r.year != null && !allowed.has(String(r.year)));
  if (outside.length) {
    issues.push({
      type: 'year_mismatch',
      count: outside.length,
      sample: outside.slice(0, 3).map((r) => ({ trade_no: r.trade_no, created_at: r.created_at })),
    });
  }
  if (parsed.preamble.begin && parsed.preamble.begin !== begin) {
    issues.push({ type: 'range_begin_mismatch', expected: begin, got: parsed.preamble.begin });
  }
  // The server may echo the exclusive end shifted by a day, so accept
  // end, end-1 and end+1 before flagging.
  if (
    parsed.preamble.end &&
    parsed.preamble.end !== end &&
    parsed.preamble.end !== addDays(end, -1) &&
    parsed.preamble.end !== addDays(end, 1)
  ) {
    issues.push({ type: 'range_end_mismatch', expected: end, got: parsed.preamble.end });
  }
  if (parsed.stats.malformed > 0) issues.push({ type: 'malformed_rows', count: parsed.stats.malformed });
  const badAmounts = parsed.records.filter((r) => r.amount == null).length;
  if (badAmounts) issues.push({ type: 'invalid_amounts', count: badAmounts });
  return { issues, empty: false };
}

function saveRawEvidence({ home, label, buffer, csvBuffer, fileName }) {
  const L = ensureLayout(home);
  ensureDir(L.raw, 0o700);
  const base = `${label}-${stampCompact()}`;
  const zipPath = path.join(L.raw, `${base}.zip`);
  writeFileAtomic(zipPath, buffer, 0o600);
  let csvPath = null;
  if (csvBuffer) {
    csvPath = path.join(L.raw, `${base}.csv`);
    writeFileAtomic(csvPath, csvBuffer, 0o600);
  }
  return { label: base, zipPath, csvPath, zipName: fileName || path.basename(zipPath), sha256: sha256(buffer), bytes: buffer.length };
}

function touchAttempt(home, ts, extra) {
  try {
    const meta = readMeta(home);
    writeMeta(home, { ...meta, lastAttemptAt: new Date(ts).toISOString(), ...(extra || {}) });
  } catch {
    /* bookkeeping must never fail a sync */
  }
}

/**
 * Run the sync.
 * @param {{home:string, from?:number, to?:number, force?:boolean,
 *          spanYears?:number, throttleSeconds?:number, maxRetries?:number,
 *          log?:Function, browser:object, ensureSession:Function}} opts
 */

/** A pending export window: one year, a span of years, or a sub-year part. */
function makeWindow(years, { today = shanghaiToday() } = {}) {
  const first = years[0];
  const last = years[years.length - 1];
  const begin = `${first}-01-01`;
  const end = last >= currentShanghaiYear(today) ? addDays(today, 1) : `${last + 1}-01-01`;
  const label = first === last ? String(first) : `${first}-${last}`;
  return { years, begin, end, label };
}

function isFullYearWindow(w, { today = shanghaiToday() } = {}) {
  if (!w.years || !w.years.length) return false;
  const first = w.years[0];
  const last = w.years[w.years.length - 1];
  if (w.begin !== `${first}-01-01`) return false;
  if (w.years.length === 1) {
    return w.end === `${first + 1}-01-01` || w.end === addDays(today, 1);
  }
  return w.end === `${last + 1}-01-01` || w.end === addDays(today, 1);
}

/**
 * Halve a window at a month boundary. Returns null when the window is already
 * small enough that splitting would not help (the service rejects on size, and
 * one-month windows are known to render).
 */
function splitWindow(w) {
  const b = Date.parse(`${w.begin}T00:00:00Z`);
  const e = Date.parse(`${w.end}T00:00:00Z`);
  if (!Number.isFinite(b) || !Number.isFinite(e)) return null;
  const days = (e - b) / 86400000;
  if (!(days > 32)) return null;
  const mid = new Date(b + Math.floor(days / 2) * 86400000).toISOString().slice(0, 7);
  const cut = `${mid}-01`;
  if (cut <= w.begin || cut >= w.end) return null;
  return [
    { ...w, begin: w.begin, end: cut, label: `${w.years.join('-')} ${w.begin}..${cut}`, split: true },
    { ...w, begin: cut, end: w.end, label: `${w.years.join('-')} ${cut}..${w.end}`, split: true },
  ];
}


/** End of a year's window: tomorrow for the current year, Jan 1 otherwise. */
function yearEndFor(year, today = shanghaiToday()) {
  return year >= currentShanghaiYear(today) ? addDays(today, 1) : `${year + 1}-01-01`;
}

/**
 * Complement of already-exported parts inside [begin, end). Used so a resumed
 * sync only asks Alipay for the months it does not have yet.
 */
function missingRanges(begin, end, parts = []) {
  const clamp = (d) => (d < begin ? begin : d > end ? end : d);
  const cuts = new Set([begin, end]);
  for (const p of parts) {
    if (!p || typeof p.begin !== 'string' || typeof p.end !== 'string') continue;
    cuts.add(clamp(p.begin));
    cuts.add(clamp(p.end));
  }
  const sorted = [...cuts].sort();
  const out = [];
  for (let i = 0; i + 1 < sorted.length; i++) {
    const a = sorted[i];
    const b = sorted[i + 1];
    if (a >= b) continue;
    const covered = parts.some((p) => p && p.begin <= a && p.end >= b);
    if (!covered) out.push({ begin: a, end: b });
  }
  return out;
}

function mergeParts(parts, next) {
  const all = [...parts, next].filter((p) => p && p.begin && p.end).sort((x, y) => (x.begin < y.begin ? -1 : 1));
  const out = [];
  for (const p of all) {
    const last = out[out.length - 1];
    if (last && p.begin <= last.end) last.end = p.end > last.end ? p.end : last.end;
    else out.push({ ...p });
  }
  return out;
}

/** Build the export windows for the planned years from their covered parts. */
function planWindows({ planned, meta, spanYears = 1, today = shanghaiToday() }) {
  const windows = [];
  for (const y of planned) {
    const begin = `${y}-01-01`;
    const end = yearEndFor(y, today);
    const parts = (meta.years && meta.years[String(y)] && meta.years[String(y)].parts) || [];
    const missing = missingRanges(begin, end, parts);
    for (const r of missing) {
      const wholeYear = missing.length === 1 && r.begin === begin && r.end === end;
      windows.push({
        years: [y],
        begin: r.begin,
        end: r.end,
        label: wholeYear ? String(y) : `${y} ${r.begin}..${r.end}`,
      });
    }
  }
  // Keep multi-year windows only when every planned year is untouched and the
  // caller explicitly asked for spans; partial years always export per range.
  if (spanYears > 1) {
    const merged = [];
    for (const w of windows) {
      const y = w.years[0];
      const isFullYear = w.begin === `${y}-01-01` && w.end === `${y + 1}-01-01`;
      const prev = merged[merged.length - 1];
      if (isFullYear && prev && prev.years.length < spanYears) {
        const py = prev.years[prev.years.length - 1];
        const prevFull = prev.begin === `${prev.years[0]}-01-01` && prev.end === `${py + 1}-01-01`;
        if (prevFull && y === py + 1) {
          prev.years.push(y);
          prev.end = w.end;
          prev.label = `${prev.years[0]}-${y}`;
          continue;
        }
      }
      merged.push({ ...w, years: [...w.years] });
    }
    return merged;
  }
  return windows;
}

async function runSync(opts) {
  const {
    home,
    from,
    to,
    force = false,
    spanYears = 1,
    throttleSeconds = DEFAULT_THROTTLE,
    maxRetries = 2,
    log = () => {},
    browser,
    ensureSession,
    // Test seams: inject the transport / clock without touching the network.
    exporter = alipay.exportRange,
    sleepFn = sleep,
    backoffSeconds = BACKOFF_SECONDS,
    // Split an oversized window when the service reports 系统繁忙 (default on).
    adaptive = true,
    // Test seam: the real implementation navigates the record page.
    ensureRecordView = alipay.ensurePersonalRecordView,
  } = opts;

  const t0 = Date.now();
  const meta = readMeta(home);
  const { planned, skipped } = planYears({ from, to, meta, force });
  const windows = planWindows({ planned, meta, spanYears });
  log(`plan: ${planned.length} year(s) in ${windows.length} export(s)${skipped.length ? `, ${skipped.length} already done` : ''}`);
  const baseResult = {
    years: [],
    skipped,
    failed: [],
    pending: [],
    added: 0,
    updated: 0,
    records: meta.records || 0,
    rateLimited: false,
  };
  if (!windows.length) return { ...baseResult, elapsedMs: Date.now() - t0 };

  const ready = await ensureSession({ browser });
  const page = ready.page;
  // Never export from the merchant record view: same host, different dataset.
  const view = await ensureRecordView({ page, log });
  if (!view.ok) {
    throw new CliError(codes.SECURITY_CHALLENGE, 'could not open the personal record view', {
      url: safeUrl(view.url),
      hint: 'open https://consumeprod.alipay.com/record/standard.htm in the alipay-cli Chrome window and click 切换到标准版, then retry',
    });
  }
  const throttleMs = Math.max(0, throttleSeconds) * 1000;
  let lastAttemptAt = meta.lastAttemptAt ? Date.parse(meta.lastAttemptAt) : 0;

  const done = [];
  const failed = [];
  let pass = 0;
  let splitBudget = 6;
  let rateLimited = false;
  let added = 0;
  let updated = 0;
  let records = meta.records || 0;

  const throttleWait = async () => {
    if (!lastAttemptAt || throttleMs === 0) return;
    const waitMs = lastAttemptAt + throttleMs - Date.now();
    if (waitMs > 0) {
      log(`throttle: waiting ${Math.ceil(waitMs / 1000)}s before the next export`);
      await sleepFn(waitMs);
    }
  };

  // Each pending item is a date window: a year (or a span of years), or a
  // sub-year part produced by splitting a window that the service refuses to
  // generate while it is busy. Splitting is the documented fallback because the
  // "系统繁忙" rejection is size-dependent: 1/3/6-month ranges render while a
  // 10-month range of the same account returns error.htm.
  let pending = windows;
  while (pending.length && pass <= maxRetries) {
    const retry = [];
    let blocked = false;
    for (const w of pending) {
      const { begin, end, label, years } = w;
      if (blocked) {
        retry.push(w);
        continue;
      }

      log(`export ${label}: ${begin} .. ${end}${pass ? ` (retry ${pass}/${maxRetries})` : ''}`);
      await throttleWait();
      const started = Date.now();
      const res = await exporter({ page, begin, end, log });
      lastAttemptAt = Date.now();
      touchAttempt(home, lastAttemptAt);

      if (!res.ok) {
        const entry = { year: label, code: res.code, detail: res.detail, ms: Date.now() - started };
        if (res.code === codes.NEED_LOGIN || res.code === codes.SECURITY_CHALLENGE) {
          failed.push(entry);
          throw new CliError(res.code, `sync stopped at ${label}: ${res.code}`, {
            label,
            detail: res.detail,
            completed: done,
            hint: res.code === codes.NEED_LOGIN ? 'run `alipay auth login`' : 'complete the security check in the browser window, then retry',
          });
        }
        if (res.code === codes.RATE_LIMITED) {
          rateLimited = true;
          const parts = adaptive ? splitWindow(w) : null;
          if (parts) {
            log(`rate limited at ${label}; splitting into ${parts.map((p) => `${p.begin}..${p.end}`).join(' + ')}`);
            retry.push(...parts);
          } else {
            blocked = true;
            retry.push(w);
            log(`rate limited at ${label}; window already small, backing off instead of hammering`);
          }
          continue;
        }
        failed.push(entry);
        log(`export ${label} failed (${res.code}); continuing with the remaining windows`);
        continue;
      }

      let parsed;
      let csvBuffer = null;
      try {
        const entries = readZipEntries(res.buffer);
        const csvEntry = pickCsvEntry(entries);
        if (!csvEntry) throw new Error(`no csv inside zip (entries: ${entries.map((e) => e.name).join(', ') || 'none'})`);
        // Real Alipay exports are GB18030; decodeAuto sniffs UTF-8 first so test
        // fixtures and future format changes still parse.
        csvBuffer = csvEntry.data;
        parsed = parseStatement(decodeAuto(csvBuffer));
      } catch (err) {
        const code = err.name === 'ZipError' ? codes.BAD_ZIP : err.name === 'CsvError' ? codes.BAD_CSV : codes.BAD_ZIP;
        failed.push({ year: label, code, detail: { reason: err.message } });
        log(`export ${label} produced an unusable archive (${err.message}); continuing`);
        continue;
      }
      const validation = validateStatement(parsed, { begin, end, years });
      const raw = saveRawEvidence({ home, label: label.replace(/[^\w.-]+/g, '_'), buffer: res.buffer, csvBuffer, fileName: res.fileName });
      const fullYear = isFullYearWindow(w);
      const yearsMeta = {};
      const metaNow = readMeta(home);
      for (const y of years) {
        const rows = parsed.records.filter((r) => r.year === y).length;
        const prevParts = (metaNow.years && metaNow.years[String(y)] && metaNow.years[String(y)].parts) || [];
        const parts = mergeParts(prevParts, { begin, end, rows, at: nowIso() });
        const uncovered = missingRanges(`${y}-01-01`, yearEndFor(y), parts);
        const covered = uncovered.length === 0;
        yearsMeta[String(y)] = {
          status: covered ? (rows ? 'complete' : 'empty') : 'partial',
          rows,
          begin,
          end,
          partial: !covered,
          parts,
          exportedAt: nowIso(),
          raw: path.relative(path.join(home, 'data'), raw.zipPath),
          sha256: raw.sha256,
          bytes: raw.bytes,
          exportMs: Date.now() - started,
          issues: validation.issues,
          span: years.length,
          trailer: parsed.preamble.trailer || null,
        };
      }
      const commit = commitRecords(home, parsed.records, { source: 'sync', years: yearsMeta });
      added += commit.added;
      updated += commit.updated;
      records = commit.records;
      done.push({
        year: label,
        years,
        status: parsed.records.length ? (fullYear ? 'complete' : 'partial') : fullYear ? 'empty' : 'partial',
        rows: parsed.records.length,
        rowsByYear: Object.fromEntries(years.map((y) => [String(y), yearsMeta[String(y)].rows])),
        added: commit.added,
        updated: commit.updated,
        range: { begin, end },
        raw: path.relative(home, raw.zipPath),
        bytes: raw.bytes,
        exportMs: Date.now() - started,
        issues: validation.issues,
        preamble: { begin: parsed.preamble.begin, end: parsed.preamble.end, trailer: parsed.preamble.trailer || null },
      });
      log(`export ${label}: ${parsed.records.length} rows (+${commit.added} new, ${commit.updated} updated) in ${Math.round((Date.now() - started) / 1000)}s`);
    }

    pending = retry;
    if (!pending.length) break;
    // Splitting is cheap and does not consume a retry/backoff slot: the smaller
    // windows are new work, not a repeat of the rejected one.
    if (!blocked && splitBudget > 0 && retry.length && retry.every((w) => w.split)) {
      splitBudget--;
      continue;
    }
    if (pass >= maxRetries) {
      log(`giving up on ${pending.map((w) => w.label).join(', ')} after ${pass + 1} attempt(s)`);
      break;
    }
    const waitSec = backoffSeconds[Math.min(pass, backoffSeconds.length - 1)];
    log(`backing off ${waitSec}s; ${pending.length} window(s) still pending`);
    touchAttempt(home, Date.now(), { rateLimit: { at: nowIso(), backoffSeconds: waitSec } });
    await sleepFn(waitSec * 1000);
    pass++;
  }
  return {
    ...baseResult,
    ok: failed.length === 0,
    years: done,
    failed,
    pending: pending.map((w) => ({ year: w.label, begin: w.begin, end: w.end, status: 'pending' })),
    added,
    updated,
    records,
    rateLimited,
    elapsedMs: Date.now() - t0,
  };
}

module.exports = {
  runSync,
  planYears,
  yearWindow,
  makeWindow,
  planWindows,
  missingRanges,
  mergeParts,
  splitWindow,
  isFullYearWindow,
  chunk,
  validateStatement,
  BACKOFF_SECONDS,
  DEFAULT_THROTTLE,
  MIN_DATA_YEAR,
  currentShanghaiYear,
};
