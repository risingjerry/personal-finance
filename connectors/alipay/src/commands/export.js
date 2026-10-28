'use strict';
// `alipay export` — dump a filtered slice in json / csv / ndjson.
const fs = require('node:fs');
const path = require('node:path');
const { loadBills } = require('../store');
const { normalizeFilters, filterRecords, sortByCreated, recordsToCsv, recordsToNdjson, pickColumns, EXPORT_COLUMNS } = require('../bills');
const { usage } = require('../errors');
const { writeFileAtomic, sha256, ensureDir } = require('../util');

const FORMATS = new Set(['json', 'csv', 'ndjson']);

async function run(ctx) {
  const flags = ctx.flags;
  const format = String(flags.format || 'json').toLowerCase();
  if (!FORMATS.has(format)) throw usage(`--format must be one of ${[...FORMATS].join('|')}`, { got: flags.format });
  const order = flags.order || 'asc';
  if (order !== 'asc' && order !== 'desc') throw usage('--order must be asc or desc');

  const { records, badLines } = loadBills(ctx.home);
  const filters = normalizeFilters({ from: flags.from, to: flags.to, direction: flags.direction, q: flags.q, min: flags.min, max: flags.max });
  const filtered = sortByCreated(filterRecords(records, filters), order);
  const columns = flags.columns ? String(flags.columns).split(',').map((s) => s.trim()).filter(Boolean) : null;
  if (columns) {
    const unknown = columns.filter((c) => !EXPORT_COLUMNS.includes(c));
    if (unknown.length) throw usage(`unknown --columns: ${unknown.join(',')}`, { known: EXPORT_COLUMNS });
  }
  const rows = pickColumns(filtered, columns);
  const payload =
    format === 'json' ? `${JSON.stringify(rows, null, ctx.pretty ? 2 : 0)}\n` : format === 'ndjson' ? recordsToNdjson(rows) : recordsToCsv(rows);

  const out = flags.out === undefined ? '-' : String(flags.out);
  const filterMeta = { from: flags.from ?? null, to: flags.to ?? null, direction: flags.direction ?? null, q: flags.q ?? null, min: flags.min ?? null, max: flags.max ?? null };

  if (out === '-') {
    process.stdout.write(payload);
    return { suppressEnvelope: true };
  }
  const dest = path.resolve(out);
  ensureDir(path.dirname(dest));
  writeFileAtomic(dest, payload, 0o600);
  return {
    cmd: 'export',
    data: {
      file: dest,
      format,
      rows: rows.length,
      bytes: Buffer.byteLength(payload),
      sha256: sha256(payload),
      columns: columns || EXPORT_COLUMNS,
    },
    meta: { home: ctx.home, storeRecords: records.length, corruptLines: badLines, filters: filterMeta, order },
  };
}

module.exports = { run, FORMATS };
