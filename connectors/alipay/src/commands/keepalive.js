'use strict';
// `alipay keepalive install|status|uninstall`
const keepalive = require('../keepalive');

function install(ctx) {
  const res = keepalive.install({ home: ctx.home, intervalSeconds: ctx.flags.interval });
  ctx.log(`keepalive agent ${res.loaded ? 'loaded' : 'written but not loaded'}: ${res.file}`);
  return {
    cmd: 'keepalive.install',
    data: {
      ...res,
      next: 'the agent pings the record page every interval; if the session is expired it reports need_login (see `alipay keepalive status`)',
    },
  };
}

function status(ctx) {
  return { cmd: 'keepalive.status', data: keepalive.status({ home: ctx.home }) };
}

function uninstall(ctx) {
  const res = keepalive.uninstall();
  ctx.log(`keepalive agent removed: ${res.file}`);
  return { cmd: 'keepalive.uninstall', data: res };
}

module.exports = { install, status, uninstall };
