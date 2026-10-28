'use strict';
// CSV export for C2C orders: UTF-8 BOM + CRLF so Excel/Numbers open it cleanly.
const { SIDE_LABEL, STATUS_LABEL } = require('./orders');

const PAY_LABEL = { confirmed: '已确认', unpaid: '未付款', paid: '已付款', pending: '处理中' };

function fmtTime(iso, tz) {
  if (!iso) return '';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return String(iso);
  const opts = {
    year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false,
  };
  if (tz && tz !== 'local') opts.timeZone = tz;
  const parts = new Intl.DateTimeFormat('en-CA', opts).formatToParts(d).reduce((acc, p) => {
    acc[p.type] = p.value;
    return acc;
  }, {});
  return `${parts.year}-${parts.month}-${parts.day} ${parts.hour}:${parts.minute}:${parts.second}`;
}

const COLUMNS = [
  ['订单号', (o) => o.id],
  ['公开单号', (o) => o.public_id],
  ['创建时间', (o, tz) => fmtTime(o.created_at, tz)],
  ['完成时间', (o, tz) => fmtTime(o.completed_at, tz)],
  ['方向', (o) => SIDE_LABEL[o.side] || o.side],
  ['币种', (o) => o.base_currency],
  ['数量', (o) => o.base_amount],
  ['法币', (o) => o.quote_currency],
  ['法币金额', (o) => o.quote_amount],
  ['单价', (o) => o.price],
  ['结算汇率', (o) => o.exchange_rate],
  ['状态', (o) => STATUS_LABEL[o.status] || o.status],
  ['付款状态', (o) => PAY_LABEL[o.payment_status] || o.payment_status],
  ['对手方', (o) => o.counterparty],
  ['收款方式', (o) => o.payment_method],
  ['订单标签', (o) => o.label],
  ['付款时间', (o, tz) => fmtTime(o.paid_at, tz)],
];

function cell(value) {
  const s = value == null ? '' : String(value);
  return /[",\r\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
}

function toCsv(orders, { tz = 'Asia/Shanghai' } = {}) {
  const lines = [COLUMNS.map(([name]) => cell(name)).join(',')];
  for (const o of orders) lines.push(COLUMNS.map(([, get]) => cell(get(o, tz))).join(','));
  return '\uFEFF' + lines.join('\r\n') + '\r\n';
}

const JSONL_COLUMNS = COLUMNS;

const FLOW_COLUMNS = [
  ['类型', (f) => (f.type === 'deposit' ? '充值' : '提币')],
  ['时间', (f, tz) => fmtTime(f.time, tz)],
  ['币种', (f) => f.currency],
  ['数量', (f) => f.amount],
  ['网络', (f) => f.network],
  ['地址', (f) => f.address],
  ['交易哈希', (f) => f.txid],
  ['手续费', (f) => f.fee],
  ['手续费币种', (f) => f.fee_currency],
  ['状态', (f) => f.status_text],
  ['内部转账', (f) => (f.type === 'deposit' ? (f.internal ? '是' : '否') : '')],
  ['ID', (f) => f.id],
];

function toFlowsCsv(flows, { tz = 'Asia/Shanghai' } = {}) {
  const lines = [FLOW_COLUMNS.map(([name]) => cell(name)).join(',')];
  for (const f of flows) lines.push(FLOW_COLUMNS.map(([, get]) => cell(get(f, tz))).join(','));
  return '\uFEFF' + lines.join('\r\n') + '\r\n';
}

module.exports = { COLUMNS, JSONL_COLUMNS, FLOW_COLUMNS, toCsv, toFlowsCsv, cell, fmtTime, PAY_LABEL };
