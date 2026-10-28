'use strict';
// Synthetic fixtures: a statement CSV that mirrors Alipay's layout and a zip
// writer, so the test suite never depends on real account data.
const zlib = require('node:zlib');

function crc32(buf) {
  let table = crc32.table;
  if (!table) {
    table = crc32.table = new Int32Array(256);
    for (let n = 0; n < 256; n++) {
      let c = n;
      for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
      table[n] = c;
    }
  }
  let crc = -1;
  for (let i = 0; i < buf.length; i++) crc = (crc >>> 8) ^ table[(crc ^ buf[i]) & 0xff];
  return (crc ^ -1) >>> 0;
}

const HEADER = [
  '交易号',
  '商家订单号',
  '交易创建时间',
  '付款时间',
  '最近修改时间',
  '交易来源地',
  '类型',
  '交易对方',
  '商品名称',
  '金额（元）',
  '收/支',
  '交易状态',
  '服务费（元）',
  '成功退款（元）',
  '备注',
  '资金状态',
  '',
];

const pad = (s, width = 20) => String(s).padEnd(width, ' ');

function rowValues(row) {
  return [
    `${row.trade_no}\t`,
    `${row.merchant_order_no || ''}\t`,
    `${row.created_at || ''} `,
    `${row.paid_at || ''} `,
    `${row.modified_at || row.created_at || ''} `,
    row.source || '淘宝',
    row.type || '支付宝担保交易',
    row.counterparty || 'shop',
    row.item || 'thing',
    `${row.amount}   `,
    row.direction_cn || '支出',
    row.status || '交易成功',
    `${row.fee || '0.00'}     `,
    `${row.refund || '0.00'}     `,
    row.note ? pad(row.note) : '                    ',
    row.fund_state || '已支出',
    '',
  ];
}

function statementCsv({ begin, end, rows, account = 'demo@example.com', exportedAt = '2026-01-02 03:04:05', trailer = true }) {
  const header = HEADER.map((h) => pad(h)).join(',');
  const totals = { out: { n: 0, cents: 0 }, in: { n: 0, cents: 0 } };
  const lines = [];
  for (const r of rows) {
    const cents = Math.round(Number(r.amount) * 100);
    const side = r.direction_cn || (r.direction === 'in' ? '收入' : r.direction === 'none' ? '不计收支' : '支出');
    if (side === '支出') {
      totals.out.n++;
      totals.out.cents += cents - Math.round(Number(r.refund || 0) * 100);
    } else if (side === '收入') {
      totals.in.n++;
      totals.in.cents += cents - Math.round(Number(r.refund || 0) * 100);
    }
    lines.push(rowValues({ ...r, direction_cn: side }).join(','));
  }
  const yuan = (cents) => (cents / 100).toFixed(2);
  const head = [
    '支付宝交易记录明细查询',
    `账号:[${account}]`,
    `起始日期:[${begin} 00:00:00]    终止日期:[${end} 00:00:00]`,
    '---------------------------------交易记录明细列表------------------------------------',
    header,
    ...lines,
  ];
  if (!trailer) return head.join('\r\n') + '\r\n';
  return [
    ...head,
    '',
    `待支出:0笔,0.00元`,
    `已支出:${totals.out.n}笔,${yuan(totals.out.cents)}元`,
    `待收入:0笔,0.00元`,
    `已收入:${totals.in.n}笔,${yuan(totals.in.cents)}元`,
    `导出时间:[${exportedAt}]    用户:测试`,
    '',
  ].join('\r\n');
}

function buildZip(files) {
  const chunks = [];
  const central = [];
  let offset = 0;
  for (const [name, content] of Object.entries(files)) {
    const data = Buffer.isBuffer(content) ? content : Buffer.from(content, 'utf8');
    const comp = zlib.deflateRawSync(data);
    const nameBuf = Buffer.from(name, 'utf8');
    const crc = crc32(data);
    const local = Buffer.alloc(30);
    local.writeUInt32LE(0x04034b50, 0);
    local.writeUInt16LE(20, 4);
    local.writeUInt16LE(0x800, 6);
    local.writeUInt16LE(8, 8);
    local.writeUInt32LE(crc, 14);
    local.writeUInt32LE(comp.length, 18);
    local.writeUInt32LE(data.length, 22);
    local.writeUInt16LE(nameBuf.length, 26);
    local.writeUInt16LE(0, 28);
    chunks.push(local, nameBuf, comp);

    const cen = Buffer.alloc(46);
    cen.writeUInt32LE(0x02014b50, 0);
    cen.writeUInt16LE(20, 4);
    cen.writeUInt16LE(20, 6);
    cen.writeUInt16LE(0x800, 8);
    cen.writeUInt16LE(8, 10);
    cen.writeUInt32LE(crc, 16);
    cen.writeUInt32LE(comp.length, 20);
    cen.writeUInt32LE(data.length, 24);
    cen.writeUInt16LE(nameBuf.length, 28);
    cen.writeUInt16LE(0, 30);
    cen.writeUInt16LE(0, 32);
    cen.writeUInt32LE(offset, 42);
    central.push(cen, nameBuf);
    offset += local.length + nameBuf.length + comp.length;
  }
  const cdBuf = Buffer.concat(central);
  const eocd = Buffer.alloc(22);
  eocd.writeUInt32LE(0x06054b50, 0);
  eocd.writeUInt16LE(0, 4);
  eocd.writeUInt16LE(0, 6);
  eocd.writeUInt16LE(Object.keys(files).length, 8);
  eocd.writeUInt16LE(Object.keys(files).length, 10);
  eocd.writeUInt32LE(cdBuf.length, 12);
  eocd.writeUInt32LE(offset, 16);
  eocd.writeUInt16LE(0, 20);
  return Buffer.concat([...chunks, cdBuf, eocd]);
}

function yearRows(year, count = 3) {
  const rows = [];
  for (let i = 0; i < count; i++) {
    const day = String(1 + (i % 27)).padStart(2, '0');
    rows.push({
      trade_no: `${year}0101${String(2100000000000 + i)}`,
      merchant_order_no: `T200P${year}${1000 + i}`,
      created_at: `${year}-01-${day} 10:00:0${i % 10}`,
      paid_at: `${year}-01-${day} 10:00:1${i % 10}`,
      modified_at: `${year}-01-${day} 10:00:2${i % 10}`,
      counterparty: i % 2 ? 'Shop A' : 'Shop B',
      item: `item ${i}`,
      amount: `${(i + 1) * 10}.50`,
      direction: i === count - 1 ? 'in' : 'out',
      direction_cn: i === count - 1 ? '收入' : '支出',
      fee: '0.00',
      refund: i === 0 ? '1.00' : '0.00',
      fund_state: i === count - 1 ? '已收入' : '已支出',
    });
  }
  return rows;
}

module.exports = { crc32, buildZip, statementCsv, yearRows, HEADER, rowValues };
