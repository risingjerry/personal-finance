'use strict';
// Import existing exports (zip or csv) into the local store. Used to backfill
// history from files captured earlier, without touching Alipay at all.
const fs = require('node:fs');
const path = require('node:path');
const { CliError, codes } = require('./errors');
const { readZipEntries, pickCsvEntry, isZip } = require('./zip');
const { parseStatement, decodeAuto } = require('./csv');
const { commitRecords } = require('./store');
const { layout, ensureLayout, ensureDir, writeFileAtomic, sha256, sha256File, nowIso, sanitizeLabel } = require('./util');

const ACCEPTED = /\.(zip|csv)$/i;

function expandInputs(inputs, home) {
  const defaultDir = layout(home).raw;
  const list = inputs && inputs.length ? inputs : [defaultDir];
  const files = [];
  for (const input of list) {
    const abs = path.resolve(input);
    let st;
    try {
      st = fs.statSync(abs);
    } catch {
      // The default evidence directory is created on demand; an empty store is
      // not an error, an explicitly named missing path is.
      if (path.resolve(input) === path.resolve(defaultDir)) continue;
      throw new CliError(codes.NOT_FOUND, `input not found: ${input}`, { path: abs });
    }
    if (st.isDirectory()) {
      const entries = fs.readdirSync(abs).filter((f) => ACCEPTED.test(f)).sort();
      const zipStems = new Set(entries.filter((f) => /\.zip$/i.test(f)).map((f) => f.replace(/\.zip$/i, '')));
      for (const f of entries) {
        // A .csv next to a same-named .zip is just the extracted copy of it.
        if (/\.csv$/i.test(f) && zipStems.has(f.replace(/\.csv$/i, ''))) continue;
        files.push(path.join(abs, f));
      }
    } else if (st.isFile()) {
      if (!ACCEPTED.test(abs)) throw new CliError(codes.BAD_INPUT, `unsupported file type (expected .zip or .csv): ${input}`);
      files.push(abs);
    }
  }
  return files;
}

function insideDir(child, dir) {
  const rel = path.relative(path.resolve(dir), path.resolve(child));
  return rel && !rel.startsWith('..') && !path.isAbsolute(rel);
}

// Store evidence under data/raw without duplicating identical bytes: an existing
// file with the same sha256 is reused instead of being copied again.
function ensureEvidenceFile(rawDir, base, ext, buffer) {
  const sha = sha256(buffer);
  for (let i = 0; i < 100; i++) {
    const label = i === 0 ? base : `${base}-${i}`;
    const dest = path.join(rawDir, `${label}${ext}`);
    if (!fs.existsSync(dest)) {
      writeFileAtomic(dest, buffer, 0o600);
      return { path: dest, label, created: true, sha256: sha };
    }
    try {
      if (sha256File(dest) === sha) return { path: dest, label, created: false, sha256: sha };
    } catch {
      /* unreadable existing file: try the next label */
    }
  }
  throw new CliError(codes.BAD_INPUT, `too many evidence files named ${base}${ext} in ${rawDir}`);
}

/** Does [begin,end) of a statement fully cover calendar year `year`? */
function coversYear(preamble, year) {
  if (!preamble || !preamble.begin || !preamble.end) return false;
  const yBegin = `${year}-01-01`;
  const yEnd = `${year + 1}-01-01`;
  return preamble.begin <= yBegin && (preamble.end >= yEnd || preamble.end === `${year}-12-31`);
}

/**
 * Import one file.
 * @returns {{file:string, rows:number, added:number, updated:number,
 *            years:number[], warnings:object[], raw:{zip?:string,csv?:string}}}
 */
function importFile({ file, home, year, log = () => {} }) {
  const L = ensureLayout(home);
  ensureDir(L.raw, 0o700);
  const buf = fs.readFileSync(file);
  let csvBuffer;
  let zipBuffer = null;
  let sourceName = path.basename(file);

  if (/\.zip$/i.test(file) && !isZip(buf)) {
    throw new CliError(codes.BAD_ZIP, `not a zip archive: ${sourceName}`, { file });
  }
  if (isZip(buf)) {
    let entries;
    try {
      entries = readZipEntries(buf);
    } catch (err) {
      throw new CliError(codes.BAD_ZIP, `cannot read zip ${sourceName}: ${err.message}`, { file });
    }
    const entry = pickCsvEntry(entries);
    if (!entry) throw new CliError(codes.BAD_ZIP, `zip contains no csv: ${sourceName}`, { file, entries: entries.map((e) => e.name) });
    csvBuffer = entry.data;
    zipBuffer = buf;
    sourceName = entry.name;
  } else {
    csvBuffer = buf;
  }

  let parsed;
  try {
    // Sniff UTF-8 first, then GB18030 (what Alipay itself emits).
    parsed = parseStatement(decodeAuto(csvBuffer));
  } catch (err) {
    throw new CliError(codes.BAD_CSV, `cannot parse statement ${sourceName}: ${err.message}`, { file, details: err.details });
  }
  if (!parsed.records.length) {
    throw new CliError(codes.EMPTY, `statement has no transaction rows: ${sourceName}`, { file, stats: parsed.stats });
  }

  const warnings = [];
  if (year != null) {
    for (const rec of parsed.records) {
      if (rec.year == null) rec.year = Number(year);
      if (rec.month == null && rec.year === Number(year)) rec.month = null;
    }
  }
  const years = [...new Set(parsed.records.map((r) => r.year).filter((y) => y != null))].sort();
  if (!years.length) {
    warnings.push({ type: 'unknown_year', message: 'no created_at on any row; pass --year to pin the year' });
  }

  // Trailer cross-check: Alipay's 待/已支出/收入 counts cover money-flow rows only
  // (收/支 = 支出|收入); 不计收支 rows are not counted there, so compare like with like.
  const trailer = parsed.preamble.trailer || {};
  const trailerCount = ['待支出', '已支出', '待收入', '已收入'].reduce((n, k) => n + (trailer[k]?.count || 0), 0);
  const flowRows = parsed.records.filter((r) => r.direction === 'in' || r.direction === 'out').length;
  if (trailerCount > 0 && trailerCount !== flowRows) {
    warnings.push({ type: 'row_count_mismatch', trailer: trailerCount, flowRows, total: parsed.records.length });
  }
  if (parsed.stats.malformed) warnings.push({ type: 'malformed_rows', count: parsed.stats.malformed });
  if (parsed.stats.repaired) warnings.push({ type: 'repaired_rows', count: parsed.stats.repaired });
  const badAmounts = parsed.records.filter((r) => r.amount == null).length;
  if (badAmounts) warnings.push({ type: 'invalid_amounts', count: badAmounts });
  const badDirections = parsed.records.filter((r) => (r.warnings || []).some((w) => w.field === 'direction')).length;
  if (badDirections) warnings.push({ type: 'unknown_direction', count: badDirections });

  // Keep the original bytes as evidence (0600). Files already inside data/raw
  // are referenced in place instead of copied.
  const evidence = {};
  const base = sanitizeLabel(path.basename(file).replace(ACCEPTED, ''));
  if (zipBuffer) {
    if (insideDir(file, L.raw)) {
      evidence.zip = path.resolve(file);
      // Keep the extracted CSV next to the archive as readable evidence.
      const stem = path.basename(file).replace(ACCEPTED, '');
      const csvPath = path.join(L.raw, `${stem}.csv`);
      if (!fs.existsSync(csvPath)) {
        writeFileAtomic(csvPath, csvBuffer, 0o600);
        evidence.csv = csvPath;
      } else {
        evidence.csv = csvPath;
      }
    } else {
      const saved = ensureEvidenceFile(L.raw, base, '.zip', zipBuffer);
      evidence.zip = saved.path;
      const csvSaved = ensureEvidenceFile(L.raw, saved.label, '.csv', csvBuffer);
      evidence.csv = csvSaved.path;
    }
  } else {
    evidence.csv = ensureEvidenceFile(L.raw, base, '.csv', csvBuffer).path;
  }

  const yearsMeta = {};
  for (const y of years) {
    const covered = coversYear(parsed.preamble, y);
    const rows = parsed.records.filter((r) => r.year === y).length;
    yearsMeta[String(y)] = covered
      ? { status: 'complete', rows, source: 'import', importedFrom: sourceName, importedAt: nowIso() }
      : { status: 'imported', rows, source: 'import', importedFrom: sourceName, importedAt: nowIso(), partial: true };
  }

  const commit = commitRecords(home, parsed.records, { source: 'import', years: yearsMeta });
  log(`imported ${sourceName}: ${parsed.records.length} rows (+${commit.added} new, ${commit.updated} updated)`);

  return {
    file: path.resolve(file),
    name: sourceName,
    rows: parsed.records.length,
    added: commit.added,
    updated: commit.updated,
    unchanged: commit.unchanged,
    years,
    coverage: { begin: parsed.preamble.begin, end: parsed.preamble.end },
    trailer,
    sha256: sha256(csvBuffer),
    evidence,
    warnings,
    stats: parsed.stats,
  };
}

function importFiles({ files, home, year, log = () => {} }) {
  const results = [];
  const errors = [];
  for (const file of files) {
    try {
      results.push(importFile({ file, home, year, log }));
    } catch (err) {
      if (err instanceof CliError && (err.code === codes.BAD_ZIP || err.code === codes.BAD_CSV || err.code === codes.EMPTY || err.code === codes.NOT_FOUND)) {
        errors.push({ file, code: err.code, message: err.message });
        log(`skipped ${path.basename(file)}: ${err.code} (${err.message})`);
        continue;
      }
      throw err;
    }
  }
  if (!results.length) {
    throw new CliError(codes.BAD_INPUT, 'no statement could be imported', { errors });
  }
  return {
    files: results,
    errors,
    added: results.reduce((n, r) => n + r.added, 0),
    updated: results.reduce((n, r) => n + r.updated, 0),
    rows: results.reduce((n, r) => n + r.rows, 0),
  };
}

module.exports = { expandInputs, importFile, importFiles, coversYear, ACCEPTED };
