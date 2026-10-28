'use strict';
// Alipay personal-statement CSV parser.
//
// The export is a GB18030-encoded, 17-column, comma-separated file with a small
// textual preamble, a padded header, padded data rows, and a summary trailer:
//
//   0  支付宝交易记录明细查询
//   1  账号:[...]
//   2  起始日期:[...]    终止日期:[...]
//   3  -----交易记录明细列表-----
//   4  交易号,...,资金状态,
//   5..rows
//   .. 已支出:2笔,200.00元 / 导出时间:[...]
//
// Fields are space-padded to a display width; the two ID columns are padded
// with a tab. Values containing ASCII commas are usually quoted, but we also
// repair rows whose item/note column leaked an extra delimiter.
const { normalizeAmount, isValidAmount } = require('./money');
const { CliError, codes } = require('./errors');

const HEADER_KEYS = {
  trade_no: ['交易号'],
  merchant_order_no: ['商家订单号'],
  created_at: ['交易创建时间', '交易时间', '创建时间'],
  paid_at: ['付款时间'],
  modified_at: ['最近修改时间', '修改时间'],
  source: ['交易来源地'],
  type: ['类型'],
  counterparty: ['交易对方', '对方'],
  item: ['商品名称', '商品说明'],
  amount: ['金额（元）', '金额(元)', '金额'],
  direction: ['收/支', '收支'],
  status: ['交易状态'],
  fee: ['服务费（元）', '服务费(元)', '服务费'],
  refund: ['成功退款（元）', '成功退款(元)', '成功退款'],
  note: ['备注'],
  fund_state: ['资金状态'],
};

const TRAILER_RE = /^(待支出|已支出|待收入|已收入|导出时间|温馨提示|说明)[:：]/;
const DIRECTION_MAP = { 支出: 'out', 收入: 'in', 不计收支: 'none' };

class CsvError extends Error {
  constructor(message, details) {
    super(message);
    this.name = 'CsvError';
    this.details = details;
  }
}

function decodeStatement(buf) {
  if (typeof buf === 'string') return buf.charCodeAt(0) === 0xfeff ? buf.slice(1) : buf;
  let text;
  try {
    text = new TextDecoder('gb18030').decode(buf);
  } catch (err) {
    throw new CsvError(`GB18030 decoder unavailable: ${err.message}`);
  }
  return text.charCodeAt(0) === 0xfeff ? text.slice(1) : text;
}

// Decode a file buffer whose encoding we do not control: prefer strict UTF-8,
// fall back to GB18030 (what Alipay itself exports).
function decodeAuto(buf) {
  if (typeof buf === 'string') return buf.charCodeAt(0) === 0xfeff ? buf.slice(1) : buf;
  try {
    const s = new TextDecoder('utf-8', { fatal: true }).decode(buf);
    return s.charCodeAt(0) === 0xfeff ? s.slice(1) : s;
  } catch {
    return decodeStatement(buf);
  }
}

// RFC4180-ish single-line splitter (quotes, doubled quotes, commas).
function splitCsvLine(line) {
  const out = [];
  let cur = '';
  let inQuotes = false;
  for (let i = 0; i < line.length; i++) {
    const c = line[i];
    if (inQuotes) {
      if (c === '"') {
        if (line[i + 1] === '"') {
          cur += '"';
          i++;
        } else {
          inQuotes = false;
        }
      } else {
        cur += c;
      }
    } else if (c === '"' && cur.trim() === '') {
      cur = '';
      inQuotes = true;
    } else if (c === ',') {
      out.push(cur);
      cur = '';
    } else {
      cur += c;
    }
  }
  out.push(cur);
  if (inQuotes) throw new CsvError('unterminated quoted field');
  return out;
}

const trimField = (s) => (s == null ? '' : String(s).replace(/^[\s\t]+|[\s\t]+$/g, ''));

// "2022-01-01 12:00:00" (Asia/Shanghai wall clock) -> ISO 8601 with +08:00.
function toIso8601(value) {
  const s = trimField(value);
  if (!s) return null;
  const m = /^(\d{4})-(\d{2})-(\d{2})(?:[ T](\d{2}):(\d{2})(?::(\d{2}))?)?$/.exec(s);
  if (!m) return null;
  const [, y, mo, d, hh = '00', mm = '00', ss = '00'] = m;
  return `${y}-${mo}-${d}T${hh}:${mm}:${ss}+08:00`;
}

function findHeaderIndex(lines) {
  for (let i = 0; i < Math.min(lines.length, 30); i++) {
    const line = lines[i];
    if (line.includes('交易号') && (line.includes('金额') || line.includes('收/支'))) return i;
  }
  return -1;
}

function buildColumnMap(headerFields) {
  const names = headerFields.map(trimField);
  const map = {};
  for (const [key, aliases] of Object.entries(HEADER_KEYS)) {
    for (const alias of aliases) {
      const idx = names.indexOf(alias);
      if (idx >= 0) {
        map[key] = idx;
        break;
      }
    }
  }
  return { names, map };
}

const REQUIRED = ['trade_no', 'created_at', 'amount', 'direction'];

function parsePreamble(lines, headerIndex) {
  const out = { title: null, account: null, begin: null, end: null, exportedAt: null, trailer: {} };
  for (const raw of lines.slice(0, headerIndex)) {
    const line = raw.trim();
    if (!line) continue;
    if (!out.title) out.title = line;
    const acc = /账号:\[([^\]]*)\]/.exec(line);
    if (acc) out.account = acc[1];
    const begin = /起始日期:\[(\d{4}-\d{2}-\d{2})/.exec(line);
    if (begin) out.begin = begin[1];
    const end = /终止日期:\[(\d{4}-\d{2}-\d{2})/.exec(line);
    if (end) out.end = end[1];
  }
  for (const raw of lines) {
    const line = raw.trim();
    const ex = /^导出时间:\[([^\]]+)\]/.exec(line);
    if (ex) out.exportedAt = ex[1];
    const sum = /^(待支出|已支出|待收入|已收入)[:：](\d+)笔,([\d.]+)元/.exec(line);
    if (sum) out.trailer[sum[1]] = { count: Number(sum[2]), amount: normalizeAmount(sum[3]) };
  }
  return out;
}

// Pad / repair a data row so that indexes line up with the header.
function normalizeRowFields(fields, width) {
  const repaired = fields.length !== width;
  let out;
  if (fields.length > width) {
    // Extra delimiters almost always come from an unquoted comma inside the
    // free-text item (index 8). Keep the tail columns pinned to the header.
    const tail = width - 9; // columns after 商品名称
    const head = fields.slice(0, 8);
    const item = fields.slice(8, fields.length - tail).join(',');
    out = head.concat([item], fields.slice(fields.length - tail));
  } else if (fields.length < width) {
    out = fields.concat(new Array(width - fields.length).fill(''));
  } else {
    out = fields;
  }
  return { fields: out, repaired };
}

function rowToRecord(fields, map, rawFields) {
  const get = (key) => (map[key] === undefined ? '' : trimField(fields[map[key]]));
  const warnings = [];

  const createdRaw = get('created_at');
  const paidRaw = get('paid_at');
  const modifiedRaw = get('modified_at');
  const created_at = toIso8601(createdRaw);
  const paid_at = toIso8601(paidRaw);
  const modified_at = toIso8601(modifiedRaw);
  if (createdRaw && !created_at) warnings.push({ field: 'created_at', raw: createdRaw });

  const amountRaw = get('amount');
  const amount = isValidAmount(amountRaw) ? normalizeAmount(amountRaw) : null;
  if (!amount) warnings.push({ field: 'amount', raw: amountRaw });

  const feeRaw = get('fee');
  const refundRaw = get('refund');
  const fee = isValidAmount(feeRaw) ? normalizeAmount(feeRaw) : '0.00';
  const refund = isValidAmount(refundRaw) ? normalizeAmount(refundRaw) : '0.00';
  if (feeRaw && !isValidAmount(feeRaw)) warnings.push({ field: 'fee', raw: feeRaw });
  if (refundRaw && !isValidAmount(refundRaw)) warnings.push({ field: 'refund', raw: refundRaw });

  const directionRaw = get('direction');
  let direction = DIRECTION_MAP[directionRaw];
  if (!direction) {
    direction = 'none';
    warnings.push({ field: 'direction', raw: directionRaw });
  }

  const effective = created_at || paid_at || modified_at;
  const year = effective ? Number(effective.slice(0, 4)) : null;
  const month = effective ? effective.slice(0, 7) : null;

  const trade_no = get('trade_no');
  const record = {
    id: trade_no,
    trade_no,
    merchant_order_no: get('merchant_order_no'),
    created_at,
    paid_at,
    modified_at,
    source: get('source'),
    type: get('type'),
    counterparty: get('counterparty'),
    item: get('item'),
    amount,
    direction,
    status: get('status'),
    fee,
    refund,
    note: get('note'),
    fund_state: get('fund_state'),
    year,
    month,
    raw: rawFields != null ? rawFields.map(trimField) : fields.map(trimField),
  };
  if (warnings.length) record.warnings = warnings;
  return record;
}

/**
 * Parse an Alipay statement CSV (Buffer or string).
 * @returns {{records: object[], header: string[], preamble: object,
 *            stats: {lines:number, parsed:number, skipped:number, repaired:number,
 *                    duplicates:number, malformed:number}}}
 */
function parseStatement(input) {
  const text = decodeStatement(input);
  const lines = text.split(/\r\n|\n|\r/);
  const headerIndex = findHeaderIndex(lines);
  if (headerIndex < 0) {
    throw new CsvError('statement header not found (missing 交易号/金额 columns)', { encoding: 'gb18030' });
  }
  const headerFields = splitCsvLine(lines[headerIndex]);
  const { names, map } = buildColumnMap(headerFields);
  const missing = REQUIRED.filter((k) => map[k] === undefined);
  if (missing.length) throw new CsvError(`statement is missing required columns: ${missing.join(',')}`, { header: names });

  const width = headerFields.length;
  const preamble = parsePreamble(lines, headerIndex);
  const records = [];
  const seen = new Set();
  let parsed = 0;
  let skipped = 0;
  let repaired = 0;
  let duplicates = 0;
  let malformed = 0;

  for (let i = headerIndex + 1; i < lines.length; i++) {
    const line = lines[i];
    if (!line || !line.trim()) continue;
    const trimmed = line.trim();
    if (TRAILER_RE.test(trimmed) || trimmed.startsWith('---') || trimmed.startsWith('===')) break;
    let fields;
    try {
      fields = splitCsvLine(line);
    } catch {
      malformed++;
      continue;
    }
    // A data row is any line that still splits into a record-like shape.
    // Trade numbers are NOT always numeric: refund rows look like
    // "0000000000000000000001_0000000000000000000002" or
    // "0000000000000000000003*T200P000000001" and must be kept verbatim.
    // Footer lines (待支出:/已支出:/导出时间: ...) are handled by TRAILER_RE above.
    if (fields.length < 3) {
      skipped++;
      continue;
    }
    const first = trimField(fields[0]);
    const hasAmount = map.amount !== undefined && trimField(fields[map.amount] || '');
    if (!first || (!/^[0-9]/.test(first) && !hasAmount)) {
      skipped++;
      continue;
    }
    const norm = normalizeRowFields(fields, width);
    if (norm.repaired) repaired++;
    const rec = rowToRecord(norm.fields, map, norm.repaired ? norm.fields : fields);
    if (norm.repaired) rec.row_repaired = true;
    if (!rec.trade_no) {
      skipped++;
      continue;
    }
    parsed++;
    if (seen.has(rec.trade_no)) duplicates++;
    seen.add(rec.trade_no);
    records.push(rec);
  }

  return { records, header: names, preamble, stats: { lines: lines.length, parsed, skipped, repaired, duplicates, malformed } };
}

module.exports = {
  CsvError,
  TRAILER_RE,
  DIRECTION_MAP,
  decodeStatement,
  decodeAuto,
  splitCsvLine,
  toIso8601,
  parseStatement,
};
