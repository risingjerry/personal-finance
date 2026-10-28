'use strict';
// Business-account bill archives: period planning (whole months first, daily
// fallback), CSV parsing for both bill kinds, and the official totals check.
const crypto = require('node:crypto');
const { readZipEntries } = require('../zip');
const { CliError, codes } = require('../errors');

const FUNDS_ID = '账务流水号';
const TRADE_IDS = ['支付宝交易号', '支付宝交易流水号'];

function decodeCsv(buffer) {
  const utf8 = buffer.toString('utf8');
  if (!utf8.includes('\uFFFD')) return utf8.replace(/^\uFEFF/, '');
  try {
    return new TextDecoder('gb18030').decode(buffer);
  } catch {
    return buffer.toString('latin1');
  }
}

/** Minimal quote-aware CSV line parser (comma delimiter, no embedded newlines). */
function splitCsvLine(line) {
  const out = [];
  let cur = '';
  let quoted = false;
  for (let i = 0; i < line.length; i++) {
    const ch = line[i];
    if (quoted) {
      if (ch === '"') {
        if (line[i + 1] === '"') { cur += '"'; i++; } else quoted = false;
      } else cur += ch;
      continue;
    }
    if (ch === '"') { quoted = true; continue; }
    if (ch === ',') { out.push(cur); cur = ''; continue; }
    cur += ch;
  }
  out.push(cur);
  return out;
}

function clean(value) {
  return String(value == null ? '' : value).replace(/\t+$/g, '').trim();
}

function parseTotals(text) {
  const grab = (label) => {
    const m = new RegExp(`#${label}合计[：:]\\s*(\\d+)笔[，,]共\\s*(-?[\\d.]+)元`).exec(text);
    return m ? { count: Number(m[1]), amount: m[2] } : null;
  };
  return { income: grab('收入'), expense: grab('支出') };
}

function moneyToCents(value) {
  const s = clean(value);
  if (!/^-?\d+(\.\d+)?$/.test(s)) return null;
  const neg = s.startsWith('-');
  const [int, frac = ''] = (neg ? s.slice(1) : s).split('.');
  const cents = BigInt(int) * 100n + BigInt((frac + '00').slice(0, 2));
  return neg ? -cents : cents;
}

/**
 * Parse one bill zip. Returns rows plus the provider's own totals so the caller
 * can refuse a partial/incorrect archive instead of guessing.
 */
function parseBillZip(buffer, kind) {
  const entries = readZipEntries(buffer);
  const rows = [];
  const issues = [];
  let totals = null;
  for (const entry of entries) {
    if (!/\.csv$/i.test(entry.name)) continue;
    const text = decodeCsv(entry.data);
    const lines = text.split(/\r?\n/).filter((l) => l.trim() && !l.startsWith('#'));
    if (!lines.length) continue;
    const header = splitCsvLine(lines[0]).map(clean);
    const isFunds = header.includes(FUNDS_ID);
    const tradeIdField = TRADE_IDS.find((f) => header.includes(f));
    if (!isFunds && !tradeIdField) continue;
    for (let i = 1; i < lines.length; i++) {
      const cells = splitCsvLine(lines[i]).map(clean);
      if (cells.every((c) => !c)) continue;
      if (cells[0] === '合计' || cells[0] === '总计') continue;
      const row = {};
      header.forEach((h, idx) => { row[h] = cells[idx] === undefined ? '' : cells[idx]; });
      if (isFunds) {
        const id = row[FUNDS_ID];
        if (!id) { issues.push({ type: 'missing_ledger_id', line: i + 1 }); continue; }
        const income = moneyToCents(row['收入金额（+元）']);
        const expense = moneyToCents(row['支出金额（-元）']);
        if (income === null || expense === null) { issues.push({ type: 'bad_amount', id }); continue; }
        rows.push({
          id,
          kind: 'funds',
          occurred_at: row['发生时间'] || '',
          income: (Number(income) / 100).toFixed(2),
          expense: (Number(expense) / 100).toFixed(2),
          counterparty: row['对方账号'] || '',
          business_id: row['业务流水号'] || '',
          order_id: row['商户订单号'] || '',
          item: row['商品名称'] || '',
          type: row['业务类型'] || '',
          channel: row['交易渠道'] || '',
          balance: row['账户余额（元）'] || '',
          raw: row,
        });
      } else {
        const id = crypto.createHash('sha256').update(JSON.stringify(row)).digest('hex');
        rows.push({
          id,
          kind: 'trade',
          occurred_at: row['完成时间'] || row['交易完成时间'] || row['创建时间'] || row['交易创建时间'] || '',
          income: null,
          expense: null,
          counterparty: row['对方账户'] || '',
          business_id: row[tradeIdField] || '',
          order_id: row['商户订单号'] || '',
          item: row['商品名称'] || '',
          type: row['交易类型'] || row['业务类型'] || '',
          channel: row['收款渠道'] || '',
          balance: '',
          raw: row,
        });
      }
    }
    const found = parseTotals(text);
    if (found && (found.income || found.expense)) {
      totals = totals || { income: 0n, expense: 0n, counts: { income: 0, expense: 0 }, expected: {} };
      if (found.income) { totals.expected.income = found.income; totals.counts.income = found.income.count; }
      if (found.expense) { totals.expected.expense = found.expense; totals.counts.expense = found.expense.count; }
    }
  }
  if (totals) {
    for (const r of rows) {
      if (r.kind !== 'funds') continue;
      totals.income += BigInt(Math.round(Number(r.income) * 100));
      totals.expense += BigInt(Math.round(Number(r.expense) * 100));
    }
    const cents = (s) => (s == null ? null : BigInt(Math.round(Number(s) * 100)));
    const expIncome = cents(totals.expected.income && totals.expected.income.amount);
    const expExpense = cents(totals.expected.expense && totals.expected.expense.amount);
    if (expIncome !== null && totals.income !== expIncome) {
      issues.push({ type: 'income_total_mismatch', expected: String(expIncome), actual: String(totals.income) });
    }
    if (expExpense !== null && totals.expense !== expExpense) {
      issues.push({ type: 'expense_total_mismatch', expected: String(expExpense), actual: String(totals.expense) });
    }
  }
  return { kind, rows, issues, totals };
}

/** Whole months when possible, single days for partial months and the current month. */
function planPeriods(startDate, endDate, today = new Date()) {
  const fmt = (d) => d.toISOString().slice(0, 10);
  const currentMonth = fmt(today).slice(0, 7);
  const periods = [];
  let d = new Date(`${startDate}T00:00:00Z`);
  const end = new Date(`${endDate}T00:00:00Z`);
  while (d <= end) {
    const last = new Date(Date.UTC(d.getUTCFullYear(), d.getUTCMonth() + 1, 0));
    const month = fmt(d).slice(0, 7);
    if (d.getUTCDate() === 1 && last <= end && month !== currentMonth) {
      periods.push(month);
      d = new Date(last.getTime() + 86400000);
    } else {
      periods.push(fmt(d));
      d = new Date(d.getTime() + 86400000);
    }
  }
  return periods;
}

function periodBounds(period) {
  if (/^\d{4}-\d{2}$/.test(period)) {
    const start = `${period}-01`;
    const [y, m] = period.split('-').map(Number);
    const last = new Date(Date.UTC(y, m, 0)).toISOString().slice(0, 10);
    return { start, end: last };
  }
  return { start: period, end: period };
}

module.exports = { decodeCsv, splitCsvLine, parseBillZip, parseTotals, planPeriods, periodBounds, moneyToCents };
