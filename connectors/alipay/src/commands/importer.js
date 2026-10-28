'use strict';
// `alipay import` — load existing zip/csv exports without touching Alipay.
const { acquireLock } = require('../lock');
const { expandInputs, importFiles } = require('../importer');
const { layout } = require('../util');

async function run(ctx) {
  const inputs = ctx.positionals.slice();
  const yearFlag = ctx.flags.year === undefined ? null : Number(ctx.flags.year);
  if (ctx.flags.year !== undefined && (!Number.isInteger(yearFlag) || yearFlag < 2000 || yearFlag > 2100)) {
    throw require('../errors').usage(`--year must be a 4-digit year, got ${ctx.flags.year}`);
  }
  const files = expandInputs(inputs, ctx.home);
  if (!files.length) {
    throw require('../errors').usage('no .zip/.csv statements found', { inputs: inputs.length ? inputs : [layout(ctx.home).raw] });
  }
  const lock = acquireLock(ctx.home, { cmd: 'import' });
  try {
    ctx.log(`importing ${files.length} file(s)`);
    const res = importFiles({ files, home: ctx.home, year: yearFlag, log: ctx.log });
    return {
      cmd: 'import',
      data: res,
      meta: { home: ctx.home, year: yearFlag, fileCount: files.length },
    };
  } finally {
    lock.release();
  }
}

module.exports = { run };
