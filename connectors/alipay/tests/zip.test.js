'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const os = require('node:os');
const fs = require('node:fs');
const { readZipEntries, pickCsvEntry, isZip, ZipError } = require('../src/zip');
const { buildZip } = require('./helpers/fixtures');

test('reads a synthetic deflate zip', () => {
  const csv = 'a,b\n1,2\n';
  const zip = buildZip({ 'record.csv': csv });
  assert.equal(isZip(zip), true);
  const entries = readZipEntries(zip);
  assert.equal(entries.length, 1);
  assert.equal(entries[0].name, 'record.csv');
  assert.equal(entries[0].data.toString('utf8'), csv);
});

test('reads multiple entries and picks the largest csv', () => {
  const zip = buildZip({ 'readme.txt': 'hello', 'small.csv': 'x', 'big.csv': 'a,b\n'.repeat(50) });
  const entries = readZipEntries(zip);
  assert.equal(entries.length, 3);
  assert.equal(pickCsvEntry(entries).name, 'big.csv');
});

test('rejects non-zip and truncated buffers', () => {
  assert.equal(isZip(Buffer.from('hello')), false);
  assert.throws(() => readZipEntries(Buffer.from('not a zip at all')), ZipError);
  const zip = buildZip({ 'a.csv': 'abc' });
  assert.throws(() => readZipEntries(zip.subarray(0, zip.length - 10)), ZipError);
});

test('reads a synthetic export from a temporary directory', (t) => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'alipay-zip-test-'));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  const file = path.join(dir, 'demo.zip');
  const csv = '商品,金额\n' + '示例商品,100.00\n'.repeat(100);
  fs.writeFileSync(file, buildZip({ 'demo.csv': csv }));
  const entries = readZipEntries(fs.readFileSync(file));
  const csvEntry = pickCsvEntry(entries);
  assert.ok(csvEntry, 'expected a csv inside the export');
  assert.ok(csvEntry.data.length > 1000);
  assert.equal(csvEntry.name, 'demo.csv');
  assert.equal(csvEntry.data.toString('utf8'), csv);
});
