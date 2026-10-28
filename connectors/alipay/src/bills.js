'use strict';
// Query layer: filtering, listing, summarising and serialising records.
// Money is compared as BigInt cents; totals are decimal strings.
const { toCents, formatCents } = require('./money');
const { CliError, codes } = require('./errors');

const DIRECTIONS = new Set(['in', 'out', 'none']);

function toStartBound(from) {
  if (!from) return null;
  if (/^\d{4}$/.test(from)) return `${from}-01-01T00:00:00+08:00`;
  if (/^\d{4}-\d{2}$/.test(from)) return `${from}-01T00:00:00+08:00`;
  if (/^\d{4}-\d{2}-\d{2}$/.test(from)) return `${from}T00:00:00+08:00`;
  throw new CliError(codes.BAD_INPUT, `bad --from (expected YYYY, YYYY-MM or YYYY-MM-DD): ${from}`);
}

function toEndBound(to) {
  if (!to) return null;
  if (/^\d{4}$/.test(to)) return `${to}-12-31T23:59:59+08:00`;
  if (/^\d{4}-\d{2}$/.test(to)) {
    const [y, m] = to.split('-').map(Number);
    const lastDay = new Date(Date.UTC(y, m, 0)).getUTCDate();
    return `${to}-${String(lastDay).padStart(2, '0')}T23:59:59+08:00`;
  }
  if (/^\d{4}-\d{2}-\d{2}$/.test(to)) return `${to}T23:59:59+08:00`;
  throw new CliError(codes.BAD_INPUT, `bad --to (expected YYYY, YYYY-MM or YYYY-MM-DD): ${to}`);
}

function parseBoundAmount(value, flag) {
  if (value == null || value === '') return null;
  try {
    return toCents(String(value));
  } catch {
    throw new CliError(codes.BAD_INPUT, `bad ${flag} amount: ${value}`);
  }
}

function normalizeFilters(filters = {}) {
  const { from, to, direction, q, min, max } = filters;
  if (direction != null && direction !== '' && !DIRECTIONS.has(direction)) {
    throw new CliError(codes.BAD_INPUT, `bad --direction: ${direction} (expected in|out|none)`);
  }
  const start = toStartBound(from);
  const end = toEndBound(to);
  if (start && end && start > end) throw new CliError(codes.BAD_INPUT, '--from is after --to');
  return {
    normalized: true,
    start,
    end,
    direction: direction || null,
    q: q ? String(q).toLowerCase() : null,
    minCents: parseBoundAmount(min, '--min'),
    maxCents: parseBoundAmount(max, '--max'),
  };
}

function recordMatches(rec, f) {
  if (f.start || f.end) {
    if (!rec.created_at) return false;
    if (f.start && rec.created_at < f.start) return false;
    if (f.end && rec.created_at > f.end) return false;
  }
  if (f.direction && rec.direction !== f.direction) return false;
  if (f.minCents != null || f.maxCents != null) {
    if (rec.amount == null) return false;
    const cents = toCents(rec.amount);
    if (f.minCents != null && cents < f.minCents) return false;
    if (f.maxCents != null && cents > f.maxCents) return false;
  }
  if (f.q) {
    const hay = [rec.counterparty, rec.item, rec.note, rec.type, rec.source, rec.trade_no, rec.merchant_order_no, rec.status]
      .filter(Boolean)
      .join('\n')
      .toLowerCase();
    if (!hay.includes(f.q)) return false;
  }
  return true;
}

function asFilters(filters) {
  return filters && filters.normalized ? filters : normalizeFilters(filters || {});
}

function filterRecords(records, filters) {
  const f = asFilters(filters);
  return records.filter((r) => recordMatches(r, f));
}

function sortByCreated(records, order = 'desc') {
  const arr = records.slice();
  arr.sort((a, b) => {
    const ka = a.created_at || '';
    const kb = b.created_at || '';
    if (ka === kb) return (a.trade_no || '') < (b.trade_no || '') ? -1 : 1;
    return order === 'asc' ? (ka < kb ? -1 : 1) : ka > kb ? -1 : 1;
  });
  return arr;
}

function listBills(records, { filters = {}, limit = 50, offset = 0, order = 'desc' } = {}) {
  const matched = filterRecords(records, filters);
  const sorted = sortByCreated(matched, order);
  const off = Math.max(0, Number(offset) || 0);
  const lim = limit == null ? 50 : Number(limit);
  const page = lim === 0 ? sorted.slice(off) : sorted.slice(off, off + Math.max(0, lim));
  return { total: matched.length, returned: page.length, offset: off, limit: lim, records: page };
}

const GROUP_BY = new Set(['year', 'month', 'counterparty', 'type', 'status', 'source']);

function groupKeyOf(rec, by) {
  switch (by) {
    case 'year':
      return rec.year == null ? 'unknown' : String(rec.year);
    case 'month':
      return rec.month || 'unknown';
    case 'counterparty':
      return rec.counterparty || '(空)';
    case 'type':
      return rec.type || '(空)';
    case 'status':
      return rec.status || '(空)';
    case 'source':
      return rec.source || '(空)';
    default:
      throw new CliError(codes.BAD_INPUT, `bad --by: ${by} (expected ${[...GROUP_BY].join('|')})`);
  }
}

function summarize(records, { by = 'year', filters = {}, top = 0 } = {}) {
  if (!GROUP_BY.has(by)) throw new CliError(codes.BAD_INPUT, `bad --by: ${by} (expected ${[...GROUP_BY].join('|')})`);
  const matched = filterRecords(records, filters);
  const groups = new Map();
  let totalIn = 0n;
  let totalOut = 0n;
  let totalNone = 0n;
  let totalRefundIn = 0n;
  let totalRefundOut = 0n;
  for (const rec of matched) {
    const key = groupKeyOf(rec, by);
    let g = groups.get(key);
    if (!g) {
      g = { key, count: 0, inCents: 0n, outCents: 0n, noneCents: 0n, refundIn: 0n, refundOut: 0n, first: null, last: null };
      groups.set(key, g);
    }
    g.count++;
    const cents = rec.amount == null ? 0n : toCents(rec.amount);
    const refundCents = rec.refund == null ? 0n : toCents(rec.refund);
    if (rec.direction === 'in') {
      g.inCents += cents;
      g.refundIn += refundCents;
      totalIn += cents;
      totalRefundIn += refundCents;
    } else if (rec.direction === 'out') {
      g.outCents += cents;
      g.refundOut += refundCents;
      totalOut += cents;
      totalRefundOut += refundCents;
    } else {
      g.noneCents += cents;
      totalNone += cents;
    }
    if (rec.created_at) {
      if (!g.first || rec.created_at < g.first) g.first = rec.created_at;
      if (!g.last || rec.created_at > g.last) g.last = rec.created_at;
    }
  }
  // Alipay's own footer totals are net of 成功退款 (verified: 已支出 == sum(out) - sum(refund out)),
  // so expose both the gross and the net-of-refund figures.
  let rows = [...groups.values()].map((g) => ({
    key: g.key,
    count: g.count,
    income: formatCents(g.inCents),
    expense: formatCents(g.outCents),
    neutral: formatCents(g.noneCents),
    net: formatCents(g.inCents - g.outCents),
    refunds_in: formatCents(g.refundIn),
    refunds_out: formatCents(g.refundOut),
    income_net: formatCents(g.inCents - g.refundIn),
    expense_net: formatCents(g.outCents - g.refundOut),
    first_at: g.first,
    last_at: g.last,
    _sort: g.inCents - g.outCents,
  }));
  if (by === 'year' || by === 'month') rows.sort((a, b) => (a.key < b.key ? -1 : a.key > b.key ? 1 : 0));
  else {
    rows.sort((a, b) => {
      const ca = a._sort < 0n ? -a._sort : a._sort;
      const cb = b._sort < 0n ? -b._sort : b._sort;
      if (ca !== cb) return cb > ca ? 1 : -1;
      return b.count - a.count;
    });
  }
  const trimmed = top > 0 ? rows.slice(0, top) : rows;
  for (const r of trimmed) delete r._sort;
  return {
    by,
    groups: trimmed,
    groupCount: rows.length,
    totals: {
      matched: matched.length,
      income: formatCents(totalIn),
      expense: formatCents(totalOut),
      neutral: formatCents(totalNone),
      net: formatCents(totalIn - totalOut),
      refunds_in: formatCents(totalRefundIn),
      refunds_out: formatCents(totalRefundOut),
      income_net: formatCents(totalIn - totalRefundIn),
      expense_net: formatCents(totalOut - totalRefundOut),
    },
  };
}

const EXPORT_COLUMNS = [
  'trade_no',
  'merchant_order_no',
  'created_at',
  'paid_at',
  'modified_at',
  'source',
  'type',
  'counterparty',
  'item',
  'amount',
  'direction',
  'status',
  'fee',
  'refund',
  'note',
  'fund_state',
  'year',
];

function csvEscape(value) {
  if (value == null) return '';
  const s = String(value);
  return /[",\n\r]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
}

function recordsToCsv(records) {
  const lines = [EXPORT_COLUMNS.join(',')];
  for (const rec of records) {
    lines.push(EXPORT_COLUMNS.map((c) => csvEscape(rec[c])).join(','));
  }
  return lines.join('\r\n') + '\r\n';
}

function recordsToNdjson(records) {
  return records.map((r) => JSON.stringify(r)).join('\n') + (records.length ? '\n' : '');
}

function pickColumns(records, columns) {
  if (!columns || !columns.length) return records;
  return records.map((rec) => {
    const out = {};
    for (const c of columns) out[c] = rec[c] === undefined ? null : rec[c];
    return out;
  });
}

module.exports = {
  DIRECTIONS,
  GROUP_BY,
  EXPORT_COLUMNS,
  normalizeFilters,
  asFilters,
  filterRecords,
  sortByCreated,
  listBills,
  summarize,
  groupKeyOf,
  recordsToCsv,
  recordsToNdjson,
  pickColumns,
  toStartBound,
  toEndBound,
};
