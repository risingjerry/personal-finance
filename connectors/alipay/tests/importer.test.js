'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { importFile, importFiles, expandInputs, coversYear } = require('../src/importer');
const { buildZip, statementCsv, yearRows } = require('./helpers/fixtures');
const { loadBills, readMeta } = require('../src/store');
const { CliError } = require('../src/errors');

function tempHome() {
  return fs.mkdtempSync(path.join(os.tmpdir(), 'alipay-import-'));
}

function writeZip(home, name, { begin, end, rows }) {
  const csv = statementCsv({ begin, end, rows });
  const dir = path.join(home, 'inbox');
  fs.mkdirSync(dir, { recursive: true });
  const file = path.join(dir, name);
  fs.writeFileSync(file, buildZip({ [`${name.replace(/\.zip$/, '')}_1.csv`]: Buffer.from(csv, 'utf8') }));
  return file;
}

test('coversYear recognises full-year and partial statements', () => {
  assert.equal(coversYear({ begin: '2025-01-01', end: '2026-01-01' }, 2025), true);
  assert.equal(coversYear({ begin: '2024-01-01', end: '2026-01-02' }, 2024), true);
  assert.equal(coversYear({ begin: '2025-01-01', end: '2025-06-30' }, 2025), false);
  assert.equal(coversYear({ begin: '2025-03-01', end: '2026-01-01' }, 2025), false);
  assert.equal(coversYear({}, 2025), false);
});

test('imports a zip, records evidence and marks the year complete', () => {
  const home = tempHome();
  const file = writeZip(home, '2025.zip', { begin: '2025-01-01', end: '2026-01-01', rows: yearRows(2025, 3) });
  const res = importFile({ file, home });
  assert.equal(res.rows, 3);
  assert.equal(res.added, 3);
  assert.deepEqual(res.years, [2025]);
  assert.equal(loadBills(home).records.length, 3);
  const meta = readMeta(home);
  assert.equal(meta.years['2025'].status, 'complete');
  assert.equal(meta.years['2025'].rows, 3);
  assert.ok(fs.existsSync(res.evidence.zip));
  assert.ok(fs.existsSync(res.evidence.csv));

  const raw = fs.readdirSync(path.join(home, 'data', 'raw'));
  assert.equal(raw.length, 2, 'one zip + one csv of evidence');
});

test('re-importing identical bytes reuses the same evidence file', () => {
  const home = tempHome();
  const file = writeZip(home, '2025.zip', { begin: '2025-01-01', end: '2026-01-01', rows: yearRows(2025, 2) });
  const first = importFile({ file, home });
  const second = importFile({ file, home });
  assert.equal(second.added, 0);
  assert.equal(second.unchanged, 2);
  assert.equal(second.evidence.zip, first.evidence.zip);
  assert.equal(fs.readdirSync(path.join(home, 'data', 'raw')).length, 2);
});

test('a partial-year file is imported but not marked complete', () => {
  const home = tempHome();
  const file = writeZip(home, 'partial.zip', { begin: '2025-03-01', end: '2025-04-01', rows: yearRows(2025, 2) });
  const res = importFile({ file, home });
  assert.equal(res.rows, 2);
  assert.equal(readMeta(home).years['2025'].status, 'imported');
  assert.equal(readMeta(home).years['2025'].partial, true);
});

test('imports a utf-8 csv directly and keeps years from the data', () => {
  const home = tempHome();
  const dir = path.join(home, 'inbox');
  fs.mkdirSync(dir, { recursive: true });
  const csvFile = path.join(dir, '2024.csv');
  fs.writeFileSync(csvFile, statementCsv({ begin: '2024-01-01', end: '2025-01-01', rows: yearRows(2024, 2) }));
  const res = importFile({ file: csvFile, home });
  assert.equal(res.rows, 2);
  assert.deepEqual(res.years, [2024]);
  const raw = fs.readdirSync(path.join(home, 'data', 'raw'));
  assert.deepEqual(raw, ['2024.csv']);
});

test('--year fills in a year for rows without a timestamp', () => {
  const home = tempHome();
  const dir = path.join(home, 'inbox');
  fs.mkdirSync(dir, { recursive: true });
  const rows = yearRows(2023, 1).map((r) => ({ ...r, created_at: '', paid_at: '', modified_at: '' }));
  const csvFile = path.join(dir, 'no-dates.csv');
  fs.writeFileSync(csvFile, statementCsv({ begin: '2023-01-01', end: '2024-01-01', rows }));
  const res = importFile({ file: csvFile, home, year: 2023 });
  assert.equal(res.rows, 1);
  assert.equal(loadBills(home).records[0].year, 2023);
});

test('warns when the statement trailer disagrees with the parsed rows', () => {
  const home = tempHome();
  const dir = path.join(home, 'inbox');
  fs.mkdirSync(dir, { recursive: true });
  const csv = statementCsv({ begin: '2025-01-01', end: '2026-01-01', rows: yearRows(2025, 4) });
  const lines = csv.split('\r\n');
  const dataStart = lines.findIndex((l) => l.startsWith('交易号'));
  lines.splice(dataStart + 1, 1); // drop one transaction row, keep the footer
  const file = path.join(dir, 'truncated.csv');
  fs.writeFileSync(file, lines.join('\r\n'));
  const res = importFile({ file, home });
  assert.equal(res.rows, 3);
  const types = res.warnings.map((w) => w.type);
  assert.ok(types.includes('row_count_mismatch'), `expected row_count_mismatch in ${types.join(',')}`);
});

test('importFiles skips unreadable inputs and reports them', () => {
  const home = tempHome();
  const dir = path.join(home, 'inbox');
  fs.mkdirSync(dir, { recursive: true });
  const good = writeZip(home, '2025.zip', { begin: '2025-01-01', end: '2026-01-01', rows: yearRows(2025, 2) });
  const bad = path.join(dir, 'broken.zip');
  fs.writeFileSync(bad, 'this is not a zip');
  const res = importFiles({ files: [good, bad], home, log: () => {} });
  assert.equal(res.files.length, 1);
  assert.equal(res.errors.length, 1);
  assert.equal(res.errors[0].code, 'bad_zip');
  assert.equal(res.added, 2);
});

test('expandInputs expands directories and rejects unknown extensions', () => {
  const home = tempHome();
  writeZip(home, '2024.zip', { begin: '2024-01-01', end: '2025-01-01', rows: yearRows(2024, 1) });
  fs.writeFileSync(path.join(home, 'inbox', 'notes.txt'), 'x');
  const files = expandInputs([path.join(home, 'inbox')], home);
  assert.equal(files.length, 1);
  assert.ok(files[0].endsWith('2024.zip'));
  assert.throws(() => expandInputs([path.join(home, 'inbox', 'notes.txt')], home), (e) => e instanceof CliError && e.code === 'bad_input');
  assert.throws(() => expandInputs([path.join(home, 'nope-dir')], home), (e) => e.code === 'not_found');
  // No explicit inputs -> the raw evidence directory.
  const rawFiles = expandInputs([], home);
  assert.deepEqual(rawFiles, []);
});
