'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { parseStatement, decodeStatement, splitCsvLine, toIso8601, CsvError } = require('../src/csv');
const { statementCsv, yearRows, HEADER, rowValues } = require('./helpers/fixtures');

test('decodes GB18030 bytes (交易号 as BD BB D2 D7 BA C5)', () => {
  const buf = Buffer.from([0xbd, 0xbb, 0xd2, 0xd7, 0xba, 0xc5]);
  assert.equal(decodeStatement(buf), '交易号');
});

test('parses a synthetic statement end to end', () => {
  const csv = statementCsv({ begin: '2025-01-01', end: '2026-01-01', rows: yearRows(2025, 3) });
  const parsed = parseStatement(csv);
  assert.equal(parsed.records.length, 3);
  assert.equal(parsed.header.length, 17);
  assert.deepEqual(parsed.stats, { lines: parsed.stats.lines, parsed: 3, skipped: 0, repaired: 0, duplicates: 0, malformed: 0 });
  assert.equal(parsed.preamble.begin, '2025-01-01');
  assert.equal(parsed.preamble.end, '2026-01-01');

  const [first, second, third] = parsed.records;
  assert.equal(first.trade_no, '202501012100000000000');
  assert.equal(first.created_at, '2025-01-01T10:00:00+08:00');
  assert.equal(first.paid_at, '2025-01-01T10:00:10+08:00');
  assert.equal(first.direction, 'out');
  assert.equal(first.amount, '10.50');
  assert.equal(first.refund, '1.00');
  assert.equal(first.year, 2025);
  assert.equal(first.month, '2025-01');
  assert.equal(first.id, first.trade_no);
  assert.equal(first.raw.length, 17);
  assert.equal(second.direction, 'out');
  assert.equal(third.direction, 'in');
  assert.equal(third.fund_state, '已收入');
});

test('keeps refund trade numbers verbatim (underscore and asterisk forms)', () => {
  const rows = [
    { ...yearRows(2025, 1)[0], trade_no: '2025102022001100000000000001_3000000000000000001', direction: 'none', direction_cn: '不计收支', status: '退款成功', amount: '23.00' },
    { ...yearRows(2025, 1)[0], trade_no: '2024111022001100000000000002*T200P400000001', direction: 'none', direction_cn: '不计收支', status: '退款成功', amount: '9.51' },
  ];
  const parsed = parseStatement(statementCsv({ begin: '2025-01-01', end: '2026-01-01', rows }));
  assert.equal(parsed.records.length, 2);
  assert.equal(parsed.records[0].trade_no, '2025102022001100000000000001_3000000000000000001');
  assert.equal(parsed.records[1].trade_no, '2024111022001100000000000002*T200P400000001');
  assert.equal(parsed.records[0].direction, 'none');
});

test('handles quoted fields and repairs an unquoted comma inside the item', () => {
  const quoted = rowValues({ trade_no: '2025010100000000000001', created_at: '2025-01-02 03:04:05', amount: '5.00', item: '"thing, with comma"' });
  const body = (row) => statementCsv({ begin: '2025-01-01', end: '2026-01-01', rows: [], trailer: false }) + row.join(',') + '\r\n';
  let parsed = parseStatement(body(quoted));
  assert.equal(parsed.records.length, 1);
  assert.equal(parsed.records[0].item, 'thing, with comma');
  assert.equal(parsed.stats.repaired, 0);

  const fields = rowValues({ trade_no: '2025010100000000000002', created_at: '2025-01-02 03:04:05', amount: '6.00', item: 'X' });
  fields[8] = 'thing,with,commas';
  parsed = parseStatement(body(fields));
  assert.equal(parsed.records.length, 1);
  assert.equal(parsed.records[0].item, 'thing,with,commas');
  assert.equal(parsed.records[0].amount, '6.00');
  assert.equal(parsed.records[0].direction, 'out');
  assert.equal(parsed.stats.repaired, 1);
  assert.equal(parsed.records[0].row_repaired, true);
  assert.equal(parsed.records[0].raw.length, 17);
});

test('stops at the summary trailer and never invents rows', () => {
  const csv = statementCsv({ begin: '2025-01-01', end: '2026-01-01', rows: yearRows(2025, 2) });
  const parsed = parseStatement(csv);
  assert.equal(parsed.records.length, 2);
  assert.ok(parsed.preamble.trailer['已支出']);
  assert.equal(parsed.stats.skipped, 0);
});

test('coerces timestamps to +08:00 ISO and keeps blanks as null', () => {
  assert.equal(toIso8601('2025-12-31 04:26:32'), '2025-12-31T04:26:32+08:00');
  assert.equal(toIso8601(''), null);
  assert.equal(toIso8601('   '), null);
  assert.equal(toIso8601('not-a-date'), null);
  const parsed = parseStatement(statementCsv({ begin: '2025-01-01', end: '2026-01-01', rows: [{ ...yearRows(2025, 1)[0], paid_at: '' }] }));
  assert.equal(parsed.records[0].paid_at, null);
});

test('records unknown directions and invalid amounts instead of silently coercing', () => {
  const row = rowValues({ trade_no: '2025010100000000000003', created_at: '2025-01-02 03:04:05', amount: 'n/a' });
  row[10] = '莫名其妙';
  const parsed = parseStatement(statementCsv({ begin: '2025-01-01', end: '2026-01-01', rows: [], trailer: false }) + row.join(',') + '\r\n');
  const rec = parsed.records[0];
  assert.equal(rec.direction, 'none');
  assert.equal(rec.amount, null);
  const fields = rec.warnings.map((w) => w.field).sort();
  assert.deepEqual(fields, ['amount', 'direction']);
});

test('splitCsvLine honours quotes, doubled quotes and commas', () => {
  assert.deepEqual(splitCsvLine('a,b,c'), ['a', 'b', 'c']);
  assert.deepEqual(splitCsvLine('a,"b,c",d'), ['a', 'b,c', 'd']);
  assert.deepEqual(splitCsvLine('"he said ""hi""",x'), ['he said "hi"', 'x']);
  assert.deepEqual(splitCsvLine('a,\tb\t,'), ['a', '\tb\t', '']);
});

test('rejects a file without the statement header', () => {
  assert.throws(() => parseStatement('hello,world\n1,2\n'), CsvError);
});

test('works without a trailing newline and with CR-only line endings', () => {
  const csv = statementCsv({ begin: '2025-01-01', end: '2026-01-01', rows: yearRows(2025, 1) });
  assert.equal(parseStatement(csv.replace(/\r\n$/, '')).records.length, 1);
  assert.equal(parseStatement(csv.replace(/\r\n/g, '\r')).records.length, 1);
});
