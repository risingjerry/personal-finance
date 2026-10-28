'use strict';
// Local store: bills.ndjson (one normalised record per line) + meta.json.
// Writes are atomic; a failed run can never truncate existing data.
const fs = require('node:fs');
const path = require('node:path');
const { layout, ensureLayout, writeFileAtomic, sha256, nowIso } = require('./util');

const RECORD_SORT_KEYS = ['created_at', 'trade_no'];

function loadBills(home) {
  const L = layout(home);
  if (!fs.existsSync(L.bills)) return { records: [], badLines: 0, file: L.bills, exists: false };
  const text = fs.readFileSync(L.bills, 'utf8');
  const records = [];
  let badLines = 0;
  for (const line of text.split('\n')) {
    if (!line || !line.trim()) continue;
    try {
      const rec = JSON.parse(line);
      if (rec && rec.trade_no) records.push(rec);
      else badLines++;
    } catch {
      badLines++;
    }
  }
  return { records, badLines, file: L.bills, exists: true };
}

function sortRecords(records) {
  return records.slice().sort((a, b) => {
    const ka = `${a.created_at || ''}|${a.trade_no || ''}`;
    const kb = `${b.created_at || ''}|${b.trade_no || ''}`;
    return ka < kb ? -1 : ka > kb ? 1 : 0;
  });
}

function writeBills(home, records) {
  const L = ensureLayout(home);
  const sorted = sortRecords(records);
  const body = sorted.map((r) => JSON.stringify(r)).join('\n') + (sorted.length ? '\n' : '');
  writeFileAtomic(L.bills, body, 0o600);
  return { count: sorted.length, sha256: sha256(body), file: L.bills };
}

function recordVersion(rec) {
  return rec.modified_at || rec.created_at || rec.paid_at || '';
}

/**
 * Merge incoming records into the existing set, keyed by trade_no.
 * On a collision the record with the newer modified_at wins (spec rule);
 * ties keep the incoming copy but preserve the first-seen index.
 */
function mergeRecords(existing, incoming) {
  const byKey = new Map();
  for (const rec of existing) byKey.set(rec.trade_no, rec);
  let added = 0;
  let updated = 0;
  let unchanged = 0;
  for (const rec of incoming) {
    const prev = byKey.get(rec.trade_no);
    if (!prev) {
      byKey.set(rec.trade_no, rec);
      added++;
      continue;
    }
    if (recordVersion(rec) > recordVersion(prev)) {
      byKey.set(rec.trade_no, rec);
      updated++;
    } else {
      unchanged++;
    }
  }
  return { records: [...byKey.values()], added, updated, unchanged };
}

const EMPTY_META = {
  version: 1,
  createdAt: null,
  updatedAt: null,
  records: 0,
  years: {},
  lastSyncAt: null,
  lastAttemptAt: null,
  throttleSeconds: null,
  rateLimit: null,
  stats: { imports: 0, exports: 0 },
};

function readMeta(home) {
  const L = layout(home);
  if (!fs.existsSync(L.meta)) return { ...EMPTY_META, years: {} };
  try {
    const meta = JSON.parse(fs.readFileSync(L.meta, 'utf8'));
    return { ...EMPTY_META, ...meta, years: meta.years || {} };
  } catch {
    // Corrupt meta must not block reads of the data itself.
    return { ...EMPTY_META, years: {}, corrupted: true };
  }
}

function writeMeta(home, meta) {
  const L = ensureLayout(home);
  const next = { ...meta, updatedAt: nowIso() };
  if (!next.createdAt) next.createdAt = next.updatedAt;
  writeFileAtomic(L.meta, JSON.stringify(next, null, 2) + '\n', 0o600);
  return next;
}

function updateMeta(home, patch) {
  const meta = readMeta(home);
  const next = { ...meta, ...patch };
  if (patch.years) next.years = { ...meta.years, ...patch.years };
  return writeMeta(home, next);
}

/**
 * Persist merged records + refresh meta counters.
 * @returns {{added:number, updated:number, unchanged:number, records:number, meta:object}}
 */
function commitRecords(home, incoming, { source, years } = {}) {
  const current = loadBills(home);
  const merged = mergeRecords(current.records, incoming);
  const written = writeBills(home, merged.records);
  const meta = readMeta(home);
  const nextMeta = {
    ...meta,
    records: written.count,
    years: { ...meta.years },
    stats: { ...EMPTY_META.stats, ...(meta.stats || {}) },
  };
  if (years) {
    for (const [year, info] of Object.entries(years)) {
      nextMeta.years[year] = { ...(meta.years[year] || {}), ...info };
    }
  }
  if (source === 'import') nextMeta.stats.imports = (nextMeta.stats.imports || 0) + 1;
  if (source === 'sync') nextMeta.stats.exports = (nextMeta.stats.exports || 0) + 1;
  nextMeta.records = written.count;
  const savedMeta = writeMeta(home, nextMeta);
  return {
    added: merged.added,
    updated: merged.updated,
    unchanged: merged.unchanged,
    records: written.count,
    sha256: written.sha256,
    meta: savedMeta,
  };
}

function yearSummary(records) {
  const byYear = new Map();
  for (const r of records) {
    if (r.year == null) continue;
    const entry = byYear.get(r.year) || { year: r.year, count: 0 };
    entry.count++;
    byYear.set(r.year, entry);
  }
  return [...byYear.values()].sort((a, b) => a.year - b.year);
}

module.exports = {
  loadBills,
  writeBills,
  sortRecords,
  mergeRecords,
  recordVersion,
  readMeta,
  writeMeta,
  updateMeta,
  commitRecords,
  yearSummary,
  EMPTY_META,
};
