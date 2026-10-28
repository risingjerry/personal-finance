'use strict';
// `alipay mcp` — optional stdio MCP server exposing the read-only query tools.
// Wire format: newline-delimited JSON-RPC 2.0 (MCP stdio transport).
const readline = require('node:readline');
const { loadBills, readMeta } = require('../store');
const { listBills, summarize, normalizeFilters } = require('../bills');
const { layout } = require('../util');
const pkg = require('../../package.json');

const SERVER_INFO = { name: 'alipay-cli', version: pkg.version };

const TOOLS = [
  {
    name: 'bills_list',
    description:
      'List personal Alipay transactions from the local store (no network). Amounts are decimal strings; direction is in|out|none.',
    inputSchema: {
      type: 'object',
      additionalProperties: false,
      properties: {
        from: { type: 'string', description: 'inclusive start date: YYYY, YYYY-MM or YYYY-MM-DD' },
        to: { type: 'string', description: 'inclusive end date: YYYY, YYYY-MM or YYYY-MM-DD' },
        direction: { type: 'string', enum: ['in', 'out', 'none'] },
        q: { type: 'string', description: 'case-insensitive substring over counterparty/item/note/type/source/trade_no' },
        min: { type: 'number', description: 'minimum amount in yuan' },
        max: { type: 'number', description: 'maximum amount in yuan' },
        limit: { type: 'integer', minimum: 1, maximum: 500, default: 50 },
        offset: { type: 'integer', minimum: 0, default: 0 },
        order: { type: 'string', enum: ['asc', 'desc'], default: 'desc' },
      },
    },
  },
  {
    name: 'bills_summary',
    description: 'Aggregate local Alipay transactions by year, month, counterparty, type, status or source. Exact decimal totals.',
    inputSchema: {
      type: 'object',
      additionalProperties: false,
      properties: {
        by: { type: 'string', enum: ['year', 'month', 'counterparty', 'type', 'status', 'source'], default: 'year' },
        from: { type: 'string' },
        to: { type: 'string' },
        direction: { type: 'string', enum: ['in', 'out', 'none'] },
        top: { type: 'integer', minimum: 0, default: 0, description: 'keep only the top N groups (0 = all)' },
      },
    },
  },
  {
    name: 'bills_status',
    description: 'Local store status: record count, years synced, last sync time, rate-limit state. Read-only, no network.',
    inputSchema: { type: 'object', additionalProperties: false, properties: {} },
  },
];

function callTool(name, args, { home }) {
  if (name === 'bills_list') {
    const { records, badLines } = loadBills(home);
    const res = listBills(records, {
      filters: normalizeFilters({ from: args.from, to: args.to, direction: args.direction, q: args.q, min: args.min, max: args.max }),
      limit: args.limit === undefined ? 50 : Number(args.limit),
      offset: args.offset === undefined ? 0 : Number(args.offset),
      order: args.order || 'desc',
    });
    return { ...res, storeRecords: records.length, corruptLines: badLines, dataFile: layout(home).bills };
  }
  if (name === 'bills_summary') {
    const { records } = loadBills(home);
    return summarize(records, {
      by: args.by || 'year',
      filters: { from: args.from, to: args.to, direction: args.direction },
      top: args.top === undefined ? 0 : Number(args.top),
    });
  }
  if (name === 'bills_status') {
    const { records, badLines } = loadBills(home);
    const meta = readMeta(home);
    return {
      records: records.length,
      corruptLines: badLines,
      dataFile: layout(home).bills,
      years: meta.years || {},
      lastSyncAt: meta.lastSyncAt || null,
      lastAttemptAt: meta.lastAttemptAt || null,
      rateLimit: meta.rateLimit || null,
    };
  }
  const err = new Error(`unknown tool: ${name}`);
  err.code = -32602;
  throw err;
}

function send(obj) {
  process.stdout.write(JSON.stringify(obj) + '\n');
}

function reply(id, result) {
  send({ jsonrpc: '2.0', id, result });
}

function replyError(id, code, message) {
  send({ jsonrpc: '2.0', id, error: { code, message } });
}

function handle(msg, ctx) {
  const { id, method, params } = msg;
  if (method === 'initialize') {
    return reply(id, {
      protocolVersion: (params && params.protocolVersion) || '2024-11-05',
      capabilities: { tools: {} },
      serverInfo: SERVER_INFO,
      instructions:
        'Read-only access to locally synced personal Alipay transactions. Use bills_list / bills_summary; amounts are exact decimal strings.',
    });
  }
  if (method === 'notifications/initialized' || method === 'initialized') return undefined;
  if (method === 'ping') return reply(id, {});
  if (method === 'tools/list') return reply(id, { tools: TOOLS });
  if (method === 'tools/call') {
    const name = params && params.name;
    const args = (params && params.arguments) || {};
    try {
      const result = callTool(name, args, ctx);
      return reply(id, { content: [{ type: 'text', text: JSON.stringify(result) }], isError: false });
    } catch (err) {
      return reply(id, { content: [{ type: 'text', text: JSON.stringify({ error: err.message }) }], isError: true });
    }
  }
  if (method === 'shutdown' || method === 'exit') return undefined;
  if (id !== undefined) return replyError(id, -32601, `method not found: ${method}`);
  return undefined;
}

function serve({ home }) {
  const rl = readline.createInterface({ input: process.stdin, terminal: false });
  process.stderr.write(`[alipay] mcp server ready (stdio, home=${home})\n`);
  rl.on('line', (line) => {
    const text = line.trim();
    if (!text) return;
    let msg;
    try {
      msg = JSON.parse(text);
    } catch {
      replyError(null, -32700, 'parse error');
      return;
    }
    try {
      handle(msg, { home });
    } catch (err) {
      if (msg && msg.id !== undefined) replyError(msg.id, -32603, err.message);
    }
  });
  rl.on('close', () => {
    process.exitCode = 0;
  });
  return { running: true };
}

module.exports = { serve, TOOLS, callTool, handle };
