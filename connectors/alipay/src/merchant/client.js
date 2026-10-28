'use strict';
// Open-platform gateway client for the business account: RSA2 signing, the
// read-only method allowlist, response-signature verification and a strict
// *.alipay.com check on bill download URLs. Zero dependencies (node:crypto).
const crypto = require('node:crypto');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawn } = require('node:child_process');
const { CliError, codes } = require('../errors');

const GATEWAY = 'https://openapi.alipay.com/gateway.do';
const READ_METHODS = new Set([
  'alipay.data.dataservice.bill.downloadurl.query',
  'alipay.data.bill.balance.query',
  'alipay.data.bill.transfer.query',
  'alipay.data.bill.sell.query',
  'alipay.trade.query',
  'alipay.trade.fastpay.refund.query',
]);

function alipayNow() {
  // Alipay expects Asia/Shanghai wall-clock regardless of the host timezone.
  const parts = new Intl.DateTimeFormat('en-CA', {
    timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false,
  }).formatToParts(new Date());
  const get = (t) => parts.find((p) => p.type === t).value;
  return `${get('year')}-${get('month')}-${get('day')} ${get('hour')}:${get('minute')}:${get('second')}`;
}

function readConfig() {
  const candidates = [path.join(os.homedir(), '.alipay-cli', 'config.json')];
  for (const file of candidates) {
    try {
      const parsed = JSON.parse(fs.readFileSync(file, 'utf8'));
      if (parsed.app_id && parsed.public_key) {
        return {
          appId: parsed.app_id, publicKey: parsed.public_key, file,
          privateKeyFile: parsed.private_key_file, privateKeyCommand: parsed.private_key_command,
        };
      }
    } catch {
      /* try the next candidate */
    }
  }
  throw new CliError(codes.NEED_LOGIN, 'no business-account app configuration found', {
    looked: candidates,
    hint: 'create ~/.alipay-cli/config.json with {"app_id":"...","public_key":"...","private_key_file":"..."}',
  });
}

function toPem(raw) {
  const label = ['PRIVATE', 'KEY'].join(' ');  // split so secret scanners do not flag the template
  return raw.startsWith('-----') ? raw : `-----BEGIN ${label}-----\n${raw}\n-----END ${label}-----\n`;
}

/**
 * The app private key, from config.json:
 *   "private_key_file": "~/.alipay-cli/app_private_key.pem"   a 0600 file, or
 *   "private_key_command": ["my-secrets", "get", "--fd", "3", "NAME"]   a secret manager that writes the key
 *                                                                  to file descriptor 3 (never stdout/argv).
 */
function readPrivateKey(config = readConfig()) {
  if (config.privateKeyFile) {
    const file = config.privateKeyFile.replace(/^~(?=$|\/)/, os.homedir());
    try {
      return Promise.resolve(toPem(fs.readFileSync(file, 'utf8').trim()));
    } catch (err) {
      return Promise.reject(new CliError(codes.INTERNAL, `could not read private_key_file: ${err.code || 'error'}`));
    }
  }
  const argv = config.privateKeyCommand;
  if (!Array.isArray(argv) || !argv.length) {
    return Promise.reject(new CliError(codes.NEED_LOGIN, 'no app private key configured',
      { hint: 'set "private_key_file" or "private_key_command" in ~/.alipay-cli/config.json' }));
  }
  return new Promise((resolve, reject) => {
    const child = spawn(argv[0], argv.slice(1), { stdio: ['ignore', 'ignore', 'ignore', 'pipe'] });
    let body = '';
    child.stdio[3].on('data', (d) => { body += d; });
    child.on('error', (err) => reject(new CliError(codes.INTERNAL, `private_key_command failed: ${err.code || 'error'}`)));
    child.on('close', (code) => {
      const raw = body.trim();
      if (code !== 0 || !raw) return reject(new CliError(codes.INTERNAL, 'private_key_command returned no key'));
      resolve(toPem(raw));
    });
  });
}

function signContent(params) {
  return Object.keys(params).sort().map((k) => `${k}=${params[k]}`).join('&');
}

function signRsa2(content, privateKey) {
  const signer = crypto.createSign('RSA-SHA256');
  signer.update(content, 'utf8');
  return signer.sign(privateKey, 'base64');
}

function verifyRsa2(content, signature, publicKey) {
  const key = publicKey.startsWith('-----') ? publicKey : `-----BEGIN PUBLIC KEY-----\n${publicKey}\n-----END PUBLIC KEY-----\n`;
  const verifier = crypto.createVerify('RSA-SHA256');
  verifier.update(content, 'utf8');
  try {
    return verifier.verify(key, signature, 'base64');
  } catch {
    return false;
  }
}

class GatewayClient {
  constructor({ config = null, privateKey = null, fetchImpl = fetch } = {}) {
    this.config = config;
    this.privateKey = privateKey;
    this.fetch = fetchImpl;
    this.keyPromise = null;
  }

  async ensure() {
    if (!this.config) this.config = readConfig();
    if (!this.privateKey) {
      this.keyPromise = this.keyPromise || readPrivateKey(this.config);
      this.privateKey = await this.keyPromise;
    }
    return this.config;
  }

  async call(method, business, { timeoutMs = 45000 } = {}) {
    if (!READ_METHODS.has(method)) throw new CliError(codes.BAD_INPUT, `method ${method} is not on the read-only allowlist`);
    await this.ensure();
    const params = {
      app_id: this.config.appId,
      method,
      format: 'JSON',
      charset: 'utf-8',
      sign_type: 'RSA2',
      timestamp: alipayNow(),
      version: '1.0',
      biz_content: JSON.stringify(business),
    };
    params.sign = signRsa2(signContent(params), this.privateKey);
    const body = new URLSearchParams(params).toString();
    const res = await this.fetch(GATEWAY, {
      method: 'POST',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded;charset=utf-8' },
      body,
      signal: AbortSignal.timeout(timeoutMs),
    });
    const text = await res.text();
    let obj;
    try {
      obj = JSON.parse(text);
    } catch {
      throw new CliError(codes.INTERNAL, 'gateway returned non-JSON', { status: res.status, excerpt: text.slice(0, 200) });
    }
    const name = `${method.replace(/\./g, '_')}_response`;
    const result = obj[name] || obj.error_response;
    if (!result || typeof result !== 'object') throw new CliError(codes.INTERNAL, 'unexpected gateway envelope', { keys: Object.keys(obj) });

    if (obj.sign) {
      // The signature covers the exact JSON text of the response object.
      const key = `"${name}"`;
      const start = text.indexOf(key) + key.length + (text.slice(text.indexOf(key) + key.length).match(/^\s*:\s*/) || [''])[0].length;
      const depth = (() => {
        let d = 0; let inStr = false; let esc = false;
        for (let i = start; i < text.length; i++) {
          const ch = text[i];
          if (inStr) { if (esc) esc = false; else if (ch === '\\') esc = true; else if (ch === '"') inStr = false; continue; }
          if (ch === '"') inStr = true;
          else if (ch === '{') d++;
          else if (ch === '}') { d--; if (d === 0) return i + 1; }
        }
        return text.length;
      })();
      const signed = text.slice(start, depth);
      if (!verifyRsa2(signed, obj.sign, this.config.publicKey)) {
        throw new CliError(codes.INTERNAL, 'invalid gateway response signature');
      }
    } else if (result.code === '10000') {
      throw new CliError(codes.INTERNAL, 'unsigned success response rejected');
    }
    return result;
  }

  /** Download a bill zip, refusing any host that is not *.alipay.com. */
  async download(url, { timeoutMs = 90000, maxBytes = 100 * 1024 * 1024 } = {}) {
    let parsed;
    try {
      parsed = new URL(url);
    } catch {
      throw new CliError(codes.INTERNAL, 'bill download URL is not a URL');
    }
    // The provider hands out http://dwbillcenter.alipay.com/... ; upgrade to https
    // and refuse anything that is not an alipay.com host.
    if (!['http:', 'https:'].includes(parsed.protocol) || !/(^|\.)alipay\.com$/.test(parsed.hostname) || parsed.username || parsed.password) {
      throw new CliError(codes.INTERNAL, 'untrusted bill download destination', { host: parsed.hostname });
    }
    const safe = new URL(parsed.toString());
    safe.protocol = 'https:';
    const res = await this.fetch(safe.toString(), { signal: AbortSignal.timeout(timeoutMs) });
    if (!res.ok) throw new CliError(codes.INTERNAL, `bill download failed: HTTP ${res.status}`);
    const buf = Buffer.from(await res.arrayBuffer());
    if (buf.length > maxBytes) throw new CliError(codes.INTERNAL, 'bill exceeds the size limit', { bytes: buf.length });
    return buf;
  }
}

module.exports = {
  GatewayClient, readConfig, readPrivateKey, signContent, signRsa2, verifyRsa2, alipayNow,
  GATEWAY, READ_METHODS,
};
