'use strict';
// Normalization, local store, queries and aggregation for C2C orders.
// Amounts stay decimal strings end to end (no float math, no rounding drift).
const fs = require('node:fs');
const { layout, writeFileAtomic, readJson, nowIso, sumDecimals, toUnits } = require('./util');

const SIDE_LABEL = { buy: '买入', sell: '卖出' };
const STATUS_LABEL = { completed: '已完成', cancelled: '已取消', pending: '进行中' };

function isoFrom(ms) {
  if (!ms || !Number.isFinite(Number(ms))) return null;
  return new Date(Number(ms)).toISOString();
}

function cleanName(name) {
  return String(name || '').replace(/^"+|"+$/g, '').trim();
}

function normalize(raw) {
  return {
    id: String(raw.id),
    public_id: raw.publicTradingOrderId != null ? String(raw.publicTradingOrderId) : null,
    side: raw.side || null,
    base_currency: raw.baseCurrency ? String(raw.baseCurrency).toUpperCase() : null,
    base_amount: raw.baseAmount != null ? String(raw.baseAmount) : null,
    quote_currency: raw.quoteCurrency ? String(raw.quoteCurrency).toUpperCase() : null,
    quote_amount: raw.quoteAmount != null ? String(raw.quoteAmount) : null,
    price: raw.price != null ? String(raw.price) : null,
    exchange_rate: raw.exchangeRate != null ? String(raw.exchangeRate) : null,
    status: raw.orderStatus || null,
    payment_status: raw.paymentStatus || null,
    label: raw.orderLabelMessage || raw.orderLabel || null,
    counterparty: cleanName(raw.counterPartyName),
    payment_method: raw.receiptAccountType || null,
    created_at: isoFrom(raw.createdDate),
    completed_at: isoFrom(raw.completedDate),
    paid_at: isoFrom(raw.rawPaidDate ?? raw.orderPaidDate),
    created_ms: Number(raw.createdDate) || 0,
    completed_ms: Number(raw.completedDate) || 0,
    source: 'c2c',
    synced_at: nowIso(),
  };
}

function loadOrders(home) {
  const file = layout(home).orders;
  if (!fs.existsSync(file)) return [];
  const out = [];
  for (const line of fs.readFileSync(file, 'utf8').split('\n')) {
    const t = line.trim();
    if (!t) continue;
    try { out.push(JSON.parse(t)); } catch { /* skip corrupt line */ }
  }
  return out;
}

function saveOrders(home, orders) {
  const file = layout(home).orders;
  const sorted = [...orders].sort((a, b) => (a.created_ms - b.created_ms) || String(a.id).localeCompare(String(b.id)));
  const body = sorted.map((o) => JSON.stringify(o)).join('\n') + (sorted.length ? '\n' : '');
  writeFileAtomic(file, body, 0o600);
  return file;
}

/** Insert-or-replace by order id. Returns {added, updated, total}. */
function upsertOrders(home, incoming) {
  const existing = new Map(loadOrders(home).map((o) => [o.id, o]));
  let added = 0;
  let updated = 0;
  for (const rec of incoming) {
    const prev = existing.get(rec.id);
    if (!prev) added += 1;
    else {
      // synced_at changes on every run; it must not count as a content update.
      const a = JSON.stringify({ ...prev, synced_at: null });
      const b = JSON.stringify({ ...rec, synced_at: null });
      if (a !== b) updated += 1;
      rec.synced_at = prev.synced_at || rec.synced_at;
    }
    existing.set(rec.id, rec);
  }
  const all = [...existing.values()];
  saveOrders(home, all);
  return { added, updated, total: all.length };
}

function filterOrders(orders, { fromMs, toMs, side, status, currency, q, includeCancelled = false } = {}) {
  return orders.filter((o) => {
    if (fromMs && o.created_ms < fromMs) return false;
    if (toMs && o.created_ms > toMs) return false;
    if (side && o.side !== side) return false;
    if (status && o.status !== status) return false;
    if (!includeCancelled && o.status === 'cancelled' && !status) return false;
    if (currency && o.base_currency !== String(currency).toUpperCase()) return false;
    if (q) {
      const hay = [o.id, o.public_id, o.counterparty, o.label, o.base_currency, o.quote_currency]
        .filter(Boolean).join(' ').toLowerCase();
      if (!hay.includes(String(q).toLowerCase())) return false;
    }
    return true;
  });
}

const monthOf = (iso) => (iso ? iso.slice(0, 7) : 'unknown');

function summarize(orders, { by = 'month' } = {}) {
  const keyFn = {
    month: (o) => monthOf(o.created_at),
    side: (o) => o.side || 'unknown',
    currency: (o) => o.base_currency || 'unknown',
    status: (o) => o.status || 'unknown',
    counterparty: (o) => o.counterparty || 'unknown',
    method: (o) => o.payment_method || 'unknown',
  }[by];
  if (!keyFn) throw new Error(`unknown --by ${by}`);
  const groups = new Map();
  for (const o of orders) {
    const k = keyFn(o);
    const g = groups.get(k) || { key: k, count: 0, completed: 0, cancelled: 0, buy: 0, sell: 0, quote: [], base: [] };
    g.count += 1;
    if (o.status === 'completed') g.completed += 1;
    if (o.status === 'cancelled') g.cancelled += 1;
    if (o.side === 'buy') g.buy += 1;
    if (o.side === 'sell') g.sell += 1;
    if (o.quote_amount) g.quote.push(o.quote_amount);
    if (o.base_amount) g.base.push(o.base_amount);
    groups.set(k, g);
  }
  return [...groups.values()]
    .sort((a, b) => String(a.key).localeCompare(String(b.key)))
    .map((g) => ({
      key: g.key,
      count: g.count,
      completed: g.completed,
      cancelled: g.cancelled,
      buy: g.buy,
      sell: g.sell,
      quote_total: sumDecimals(g.quote),
      base_total: sumDecimals(g.base),
    }));
}

function totals(orders) {
  const completed = orders.filter((o) => o.status === 'completed');
  const buy = completed.filter((o) => o.side === 'buy');
  const sell = completed.filter((o) => o.side === 'sell');
  return {
    orders: orders.length,
    completed: completed.length,
    cancelled: orders.filter((o) => o.status === 'cancelled').length,
    buy_count: buy.length,
    sell_count: sell.length,
    buy_quote_total: sumDecimals(buy.map((o) => o.quote_amount)),
    sell_quote_total: sumDecimals(sell.map((o) => o.quote_amount)),
    buy_base_total: sumDecimals(buy.map((o) => o.base_amount)),
    sell_base_total: sumDecimals(sell.map((o) => o.base_amount)),
    first_at: orders.reduce((m, o) => (!m || o.created_at < m ? o.created_at : m), null),
    last_at: orders.reduce((m, o) => (!m || o.created_at > m ? o.created_at : m), null),
  };
}

module.exports = {
  SIDE_LABEL, STATUS_LABEL, normalize, loadOrders, saveOrders, upsertOrders,
  filterOrders, summarize, totals, monthOf,
};
