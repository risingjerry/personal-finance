'use strict';
// Alipay-specific knowledge: URLs, page-state classification, and the export
// request executed inside the logged-in page context.
//
// Nothing here ever performs a state-changing action: only record/*.htm GETs and
// the read-only record/download.resource POST (the statement export).
const { CliError, codes } = require('./errors');
const { alipayDate, safeUrl } = require('./util');

const ORIGIN = 'https://consumeprod.alipay.com';
const AUTH_ORIGIN = 'https://auth.alipay.com';
const DOWNLOAD_RESOURCE = `${ORIGIN}/record/download.resource`;
const STANDARD_PAGE = `${ORIGIN}/record/standard.htm`;
const DOWNLOAD_PAGE = `${ORIGIN}/record/download.htm`;
const SWITCH_VERSION = `${ORIGIN}/record/switchVersion.htm`;
const LOGIN_PAGE = AUTH_ORIGIN;


// The same account exposes two record views on the same host:
//   personal  -> standard.htm, marker "我的账单"/"切换到高级版"
//   merchant  -> advanced.htm, marker "切换到标准版" (交易管理/商家服务)
// The merchant view exports merchant trades, which must never be mixed into the
// personal bill store. switchVersion.htm toggles between them.
const PERSONAL_VIEW_RE = /我的账单|切换到高级版/;
const MERCHANT_VIEW_RE = /切换到标准版/;

function classifyRecordView(text) {
  if (MERCHANT_VIEW_RE.test(text)) return 'merchant';
  if (PERSONAL_VIEW_RE.test(text)) return 'personal';
  return 'unknown';
}

async function ensurePersonalRecordView({ page, log = () => {}, attempts = 2 }) {
  let last = { url: '', text: '' };
  for (let i = 0; i < attempts; i++) {
    await page.navigate(STANDARD_PAGE);
    last = { url: await page.currentUrl(), text: await page.bodyText() };
    const view = classifyRecordView(last.text);
    if (view === 'personal') return { ok: true, switched: i > 0, url: last.url };
    if (view !== 'merchant') break;
    log('record page opened in the merchant view; switching to the personal one');
    await page.navigate(SWITCH_VERSION);
  }
  return { ok: false, url: last.url, text: String(last.text || '').slice(0, 200) };
}

function downloadUrl({ begin, end }) {
  const q = new URLSearchParams({
    _input_charset: 'utf-8',
    suffix: 'csv',
    dateRange: 'customDate',
    dateType: 'createDate',
    fundFlow: 'all',
    beginDate: alipayDate(begin),
    endDate: alipayDate(end),
    beginTime: '00:00',
    endTime: '24:00',
    tradeType: 'ALL',
    status: 'all',
    pageNum: '1',
  });
  return `${DOWNLOAD_PAGE}?${q.toString()}`;
}

function standardUrl({ begin, end, pageNum = 1 } = {}) {
  if (!begin || !end) return STANDARD_PAGE;
  const q = new URLSearchParams({
    _input_charset: 'utf-8',
    dateRange: 'customDate',
    dateType: 'createDate',
    beginDate: alipayDate(begin),
    endDate: alipayDate(end),
    beginTime: '00:00',
    endTime: '24:00',
    tradeType: 'ALL',
    status: 'all',
    pageNum: String(pageNum),
    fundFlow: 'all',
  });
  return `${STANDARD_PAGE}?${q.toString()}`;
}

function hostOf(url) {
  try {
    return new URL(url).host;
  } catch {
    return '';
  }
}

const RE = {
  needLogin: /扫码登录|登录支付宝|请登录|账号登录|密码登录|立即登录/,
  security: /安全校验|验证身份|短信校验|请先验证|滑动验证|身份验证/,
  sessionOk: /退出|我的账单|交易记录明细|账户资产/,
  rateLimited: /系统繁忙|请稍候再试|稍后再试|访问过于频繁|操作过于频繁/,
  exportReady: /已经开始下载|已开始下载|无需短信校验|安全设置检测成功/,
  exportPending: /安全设置检测|正在检测|检测中|请稍候|正在加载/,
  exportError: /error|出错了|导出失败|下载失败/i,
};

function classifySession({ url = '', text = '' } = {}) {
  const host = hostOf(url);
  const t = String(text || '');
  if (host.includes('auth.alipay.com')) return { session: 'need_login', reason: 'redirected to auth.alipay.com' };
  if (RE.needLogin.test(t)) return { session: 'need_login', reason: 'login copy on page' };
  if (RE.security.test(t)) return { session: 'security_challenge', reason: 'security verification required' };
  if (host.includes('consumeprod.alipay.com') && RE.sessionOk.test(t)) return { session: 'ok', reason: 'session markers present' };
  return { session: 'unknown', reason: 'no recognizable markers', url, excerpt: t.slice(0, 160) };
}

function classifyDownloadPage({ url = '', text = '' } = {}) {
  const host = hostOf(url);
  const t = String(text || '');
  if (host.includes('auth.alipay.com')) return { state: 'need_login', detail: 'redirected to auth.alipay.com' };
  // Throttling may redirect to
  // https://consumeprod.alipay.com/error.htm?functionCode=FUN.ALL&causeCode=ERROR.SYSTEM_BUSY
  if (/ERROR\.SYSTEM_BUSY/i.test(url)) return { state: 'rate_limited', detail: `url: ${url.slice(0, 160)}` };
  if (RE.needLogin.test(t)) return { state: 'need_login', detail: 'login copy on page' };
  // Success first: the success copy embeds "无需短信校验", which would otherwise
  // match the security-challenge markers.
  if (RE.exportReady.test(t)) return { state: 'ready', detail: t.slice(0, 160) };
  if (RE.rateLimited.test(t)) return { state: 'rate_limited', detail: t.slice(0, 160) };
  if (RE.security.test(t) && !/无需短信校验/.test(t)) return { state: 'security_challenge', detail: t.slice(0, 160) };
  if (RE.exportError.test(t) && !RE.exportPending.test(t)) return { state: 'error', detail: t.slice(0, 160) };
  if (RE.exportPending.test(t)) return { state: 'pending', detail: t.slice(0, 160) };
  return { state: 'unknown', detail: t.slice(0, 160) };
}

// Runs inside the page: pulls securityId from the rendered form and POSTs the
// export request with the page's own credentials/cookies.
const FETCH_ZIP_EXPR = `(async () => {
  const out = (o) => o;
  const form = document.getElementById('downloadForm');
  if (!form) return out({ ok: false, reason: 'no_download_form', url: location.href, text: (document.body.innerText||'').replace(/\\s+/g,' ').slice(0,300) });
  const sec = form.querySelector('[name=securityId]') || document.querySelector('[name=securityId]');
  const sid = sec ? sec.value : '';
  if (!sid) return out({ ok: false, reason: 'no_security_id', url: location.href });
  const t0 = Date.now();
  const res = await fetch('${DOWNLOAD_RESOURCE}', {
    method: 'POST',
    credentials: 'include',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body: 'securityId=' + encodeURIComponent(sid) + '&_xbox=true&rdsToken=&rdsUa='
  });
  const ct = res.headers.get('content-type') || '';
  const cd = res.headers.get('content-disposition') || '';
  const buf = await res.arrayBuffer();
  const bytes = new Uint8Array(buf);
  let bin = '';
  const CH = 0x8000;
  for (let i = 0; i < bytes.length; i += CH) bin += String.fromCharCode.apply(null, bytes.subarray(i, i + CH));
  return out({ ok: true, status: res.status, contentType: ct, contentDisposition: cd, size: bytes.length, elapsedMs: Date.now() - t0, b64: btoa(bin) });
})()`;

async function pollBodyText(page, { timeoutMs, intervalMs = 400, until = () => false, onSample = null }) {
  const deadline = Date.now() + timeoutMs;
  let last = { url: '', text: '' };
  let samples = 0;
  while (Date.now() < deadline) {
    try {
      last = { url: await page.currentUrl(), text: await page.bodyText() };
      samples++;
      if (onSample) onSample(last);
      if (until(last)) return { ...last, samples };
    } catch (err) {
      if (err.code === codes.BROWSER_DOWN) throw err;
      // Transient context errors during navigation are expected; keep polling.
    }
    await new Promise((r) => setTimeout(r, intervalMs));
  }
  return { ...last, samples, timedOut: true };
}

/**
 * Export one date range through the logged-in page.
 * @param {{page: object, begin: string, end: string, timeoutMs?: number,
 *          checkTimeoutMs?: number, log?: Function}} opts
 * @returns {Promise<{ok:true, buffer:Buffer, fileName:string, meta:object}
 *                  | {ok:false, code:string, detail:object}>}
 */
async function exportRange({ page, begin, end, timeoutMs = 180000, checkTimeoutMs = 90000, log = () => {} }) {
  const url = downloadUrl({ begin, end });
  const t0 = Date.now();
  log(`export ${begin}..${end}: loading download.htm`);
  await page.navigate(url, { timeoutMs: 60000 });
  const observed = [];
  const start = Date.now();
  const terminal = new Set(['ready', 'rate_limited', 'security_challenge', 'need_login', 'error']);
  const pageState = await pollBodyText(page, {
    timeoutMs: checkTimeoutMs,
    until: (s) => {
      const cls = classifyDownloadPage(s);
      if (observed[observed.length - 1] !== cls.state) observed.push(cls.state);
      if (terminal.has(cls.state)) return true;
      // An unrecognised page is only fatal after a grace period: the real page
      // renders progressively and can look empty for the first few seconds.
      return cls.state === 'unknown' && Date.now() - start > 15000;
    },
  });
  const cls = classifyDownloadPage(pageState);
  const checkMs = Date.now() - t0;
  if (cls.state !== 'ready') {
    return {
      ok: false,
      code: cls.state === 'rate_limited' ? codes.RATE_LIMITED : cls.state === 'security_challenge' ? codes.SECURITY_CHALLENGE : cls.state === 'need_login' ? codes.NEED_LOGIN : codes.INTERNAL,
      detail: {
        state: cls.state,
        pageUrl: safeUrl(pageState.url),
        reason: cls.state,
        observedStates: observed,
        checkMs,
        checkTimedOut: !!pageState.timedOut,
      },
    };
  }

  log(`export ${begin}..${end}: security check passed in ${checkMs}ms, requesting zip`);
  const t1 = Date.now();
  const res = await page.evaluate(FETCH_ZIP_EXPR, { awaitPromise: true, returnByValue: true, timeoutMs });
  const fetchMs = Date.now() - t1;
  if (!res || !res.ok) {
    return { ok: false, code: res?.reason === 'no_download_form' ? codes.SECURITY_CHALLENGE : codes.INTERNAL, detail: { state: 'export_form_missing', page: res, checkMs, fetchMs } };
  }

  const buf = Buffer.from(res.b64 || '', 'base64');
  const isZipMagic = buf.length > 4 && buf[0] === 0x50 && buf[1] === 0x4b && buf[2] === 0x03 && buf[3] === 0x04;
  if (!isZipMagic) {
    const text = buf.toString('utf8');
    const cls2 = classifyDownloadPage({ url: pageState.url, text });
    let code = codes.INTERNAL;
    if (cls2.state === 'rate_limited') code = codes.RATE_LIMITED;
    else if (cls2.state === 'security_challenge') code = codes.SECURITY_CHALLENGE;
    else if (cls2.state === 'need_login') code = codes.NEED_LOGIN;
    else if (res.status !== 200) code = codes.BAD_ZIP;
    return {
      ok: false,
      code,
      detail: { state: 'not_a_zip', httpStatus: res.status, contentType: res.contentType, bytes: buf.length, checkMs, fetchMs },
    };
  }

  const fileName = parseFilename(res.contentDisposition) || `alipay_record_${Date.now()}.zip`;
  return {
    ok: true,
    buffer: buf,
    fileName,
    meta: {
      url,
      httpStatus: res.status,
      contentType: res.contentType,
      bytes: buf.length,
      checkMs,
      fetchMs,
      totalMs: Date.now() - t0,
      observedStates: observed,
    },
  };
}

function parseFilename(contentDisposition) {
  if (!contentDisposition) return null;
  const m = /filename\*?=(?:UTF-8''|")?([^";]+)/i.exec(contentDisposition);
  if (!m) return null;
  try {
    return decodeURIComponent(m[1].replace(/"$/, ''));
  } catch {
    return m[1];
  }
}

module.exports = {
  ORIGIN,
  AUTH_ORIGIN,
  LOGIN_PAGE,
  DOWNLOAD_RESOURCE,
  DOWNLOAD_PAGE,
  SWITCH_VERSION,
  classifyRecordView,
  ensurePersonalRecordView,
  STANDARD_PAGE,
  downloadUrl,
  standardUrl,
  classifySession,
  classifyDownloadPage,
  exportRange,
  pollBodyText,
  parseFilename,
  FETCH_ZIP_EXPR,
  RE,
};
