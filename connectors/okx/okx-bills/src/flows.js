'use strict';
// Normalization, store, queries and aggregation for asset flows
// (on-chain / internal deposits and withdrawals).
const fs = require('node:fs');
const { layout, writeFileAtomic, nowIso, sumDecimals } = require('./util');

const TYPE_LABEL = { deposit: '充值', withdraw: '提币' };

function isoFrom(ms) {
  const n = Number(ms);
  return Number.isFinite(n) && n > 0 ? new Date(n).toISOString() : null;
}

function normalizeDeposit(raw) {
  const ts = raw.depositInitiateTime || raw.lastUpdate;
  return {
    key: `deposit:${raw.depositId ?? raw.txid}`,
    id: String(raw.depositId ?? raw.txid ?? ''),
    type: 'deposit',
    currency: raw.currency ? String(raw.currency).toUpperCase() : null,
    amount: raw.amount != null ? String(raw.amount) : null,
    network: raw.networkName || raw.subCurrency || null,
    address: raw.fromAddress || raw.address || null,
    txid: raw.txid || null,
    fee: null,
    fee_currency: null,
    status: raw.status || null,
    status_text: raw.statusStr || raw.status || null,
    internal: !!raw.isInnerTransfer || raw.addressType === 1,
    time: isoFrom(ts),
    time_ms: Number(ts) || 0,
    source: 'asset-deposit',
    synced_at: nowIso(),
  };
}

function normalizeWithdraw(raw) {
  return {
    key: `withdraw:${raw.id ?? raw.txid}`,
    id: String(raw.id ?? raw.txid ?? ''),
    type: 'withdraw',
    currency: raw.currency ? String(raw.currency).toUpperCase() : null,
    amount: raw.amount != null ? String(raw.amount) : null,
    network: raw.networkName || raw.subCurrency || null,
    address: raw.address || null,
    txid: raw.txid || null,
    fee: raw.fee != null && raw.fee !== '' ? String(raw.fee) : null,
    fee_currency: raw.feeCurrency || null,
    status: raw.status != null ? String(raw.status) : null,
    status_text: raw.statusStr || raw.status || null,
    internal: false,
    time: isoFrom(raw.createDate),
    time_ms: Number(raw.createDate) || 0,
    source: 'asset-withdraw',
    synced_at: nowIso(),
  };
}

function loadFlows(home) {
  const file = layout(home).flows;
  if (!fs.existsSync(file)) return [];
  const out = [];
  for (const line of fs.readFileSync(file, 'utf8').split('\n')) {
    const t = line.trim();
    if (!t) continue;
    try { out.push(JSON.parse(t)); } catch { /* skip corrupt line */ }
  }
  return out;
}

function saveFlows(home, flows) {
  const file = layout(home).flows;
  const sorted = [...flows].sort((a, b) => (a.time_ms - b.time_ms) || a.key.localeCompare(b.key));
  writeFileAtomic(file, sorted.map((f) => JSON.stringify(f)).join('\n') + (sorted.length ? '\n' : ''), 0o600);
  return file;
}

function upsertFlows(home, incoming) {
  const existing = new Map(loadFlows(home).map((f) => [f.key, f]));
  let added = 0;
  let updated = 0;
  for (const rec of incoming) {
    const prev = existing.get(rec.key);
    if (!prev) added += 1;
    else {
      const a = JSON.stringify({ ...prev, synced_at: null });
      const b = JSON.stringify({ ...rec, synced_at: null });
      if (a !== b) updated += 1;
      rec.synced_at = prev.synced_at || rec.synced_at;
    }
    existing.set(rec.key, rec);
  }
  const all = [...existing.values()];
  saveFlows(home, all);
  return { added, updated, total: all.length };
}

function filterFlows(flows, { fromMs, toMs, type, currency, status, q } = {}) {
  return flows.filter((f) => {
    if (fromMs && f.time_ms < fromMs) return false;
    if (toMs && f.time_ms > toMs) return false;
    if (type && f.type !== type) return false;
    if (currency && f.currency !== String(currency).toUpperCase()) return false;
    if (status && String(f.status_text || '').includes(status)) return false;
    if (q) {
      const hay = [f.id, f.currency, f.network, f.address, f.txid, f.status_text].filter(Boolean).join(' ').toLowerCase();
      if (!hay.includes(String(q).toLowerCase())) return false;
    }
    return true;
  });
}

function summarizeFlows(flows, { by = 'month' } = {}) {
  const keyFn = {
    month: (f) => (f.time ? f.time.slice(0, 7) : 'unknown'),
    type: (f) => f.type,
    currency: (f) => f.currency || 'unknown',
    network: (f) => f.network || 'unknown',
    status: (f) => f.status_text || 'unknown',
  }[by];
  if (!keyFn) throw new Error(`unknown --by ${by}`);
  const groups = new Map();
  for (const f of flows) {
    const k = keyFn(f);
    const g = groups.get(k) || { key: k, count: 0, deposit: 0, withdraw: 0, amounts: [], fees: [] };
    g.count += 1;
    if (f.type === 'deposit') g.deposit += 1;
    if (f.type === 'withdraw') g.withdraw += 1;
    if (f.amount) g.amounts.push(f.amount);
    if (f.fee) g.fees.push(f.fee);
    groups.set(k, g);
  }
  return [...groups.values()]
    .sort((a, b) => String(a.key).localeCompare(String(b.key)))
    .map((g) => ({
      key: g.key,
      count: g.count,
      deposit: g.deposit,
      withdraw: g.withdraw,
      amount_total: sumDecimals(g.amounts),
      fee_total: sumDecimals(g.fees),
    }));
}

function totalsFlows(flows) {
  const dep = flows.filter((f) => f.type === 'deposit');
  const wd = flows.filter((f) => f.type === 'withdraw');
  return {
    flows: flows.length,
    deposits: dep.length,
    withdrawals: wd.length,
    deposit_amount: sumDecimals(dep.map((f) => f.amount)),
    withdraw_amount: sumDecimals(wd.map((f) => f.amount)),
    withdraw_fee: sumDecimals(wd.map((f) => f.fee).filter(Boolean)),
    first_at: flows.reduce((m, f) => (!m || f.time < m ? f.time : m), null),
    last_at: flows.reduce((m, f) => (!m || f.time > m ? f.time : m), null),
  };
}

module.exports = {
  TYPE_LABEL, normalizeDeposit, normalizeWithdraw, loadFlows, saveFlows,
  upsertFlows, filterFlows, summarizeFlows, totalsFlows,
};
