'use strict';
// Local store for business-account bills: one NDJSON file per kind, a coverage
// ledger per (kind, period), and the untouched provider zips as evidence.
const fs = require('node:fs');
const path = require('node:path');
const { layout, ensureDir, writeFileAtomic, nowIso, fileExists } = require('../util');

function merchantLayout(home) {
  const L = layout(home);
  const dir = path.join(L.home, 'merchant');
  return {
    dir,
    funds: path.join(dir, 'funds.ndjson'),
    trades: path.join(dir, 'trades.ndjson'),
    coverage: path.join(dir, 'coverage.json'),
    raw: path.join(dir, 'raw'),
  };
}

function ensureMerchant(home) {
  const M = merchantLayout(home);
  ensureDir(M.dir, 0o700);
  ensureDir(M.raw, 0o700);
  return M;
}

function kindFile(M, kind) {
  return kind === 'trade' ? M.trades : M.funds;
}

function loadKind(home, kind) {
  const M = merchantLayout(home);
  const file = kindFile(M, kind);
  if (!fileExists(file)) return { records: [], badLines: 0 };
  const records = [];
  let badLines = 0;
  for (const line of fs.readFileSync(file, 'utf8').split('\n')) {
    if (!line.trim()) continue;
    try {
      records.push(JSON.parse(line));
    } catch {
      badLines++;
    }
  }
  return { records, badLines };
}

function loadAll(home) {
  const funds = loadKind(home, 'funds');
  const trades = loadKind(home, 'trade');
  return {
    funds: funds.records,
    trades: trades.records,
    badLines: funds.badLines + trades.badLines,
  };
}

function writeKind(home, kind, records) {
  const M = ensureMerchant(home);
  const file = kindFile(M, kind);
  const body = records.map((r) => JSON.stringify(r)).join('\n') + (records.length ? '\n' : '');
  writeFileAtomic(file, body, 0o600);
  return { file, count: records.length };
}

/** Merge by id; a newer occurrence replaces the old row. */
function mergeKind(home, kind, incoming) {
  const { records } = loadKind(home, kind);
  const map = new Map(records.map((r) => [r.id, r]));
  let added = 0;
  let updated = 0;
  for (const row of incoming) {
    if (map.has(row.id)) {
      map.set(row.id, row);
      updated++;
    } else {
      map.set(row.id, row);
      added++;
    }
  }
  const merged = [...map.values()].sort((a, b) => (a.occurred_at < b.occurred_at ? -1 : a.occurred_at > b.occurred_at ? 1 : 0));
  writeKind(home, kind, merged);
  return { added, updated, total: merged.length };
}

function readCoverage(home) {
  const M = merchantLayout(home);
  if (!fileExists(M.coverage)) return {};
  try {
    return JSON.parse(fs.readFileSync(M.coverage, 'utf8'));
  } catch {
    return {};
  }
}

function writeCoverage(home, coverage) {
  const M = ensureMerchant(home);
  writeFileAtomic(M.coverage, JSON.stringify(coverage, null, 1) + '\n', 0o600);
  return coverage;
}

function setCoverage(home, key, info) {
  const coverage = readCoverage(home);
  coverage[key] = { ...(coverage[key] || {}), ...info, checkedAt: nowIso() };
  writeCoverage(home, coverage);
  return coverage[key];
}

function saveRaw(home, kind, period, buffer) {
  const M = ensureMerchant(home);
  const file = path.join(M.raw, `${kind}-${period}.zip`);
  writeFileAtomic(file, buffer, 0o600);
  return file;
}

module.exports = { merchantLayout, ensureMerchant, loadKind, loadAll, writeKind, mergeKind, readCoverage, writeCoverage, setCoverage, saveRaw };
