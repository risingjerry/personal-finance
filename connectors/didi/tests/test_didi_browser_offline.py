"""Optional offline browser tests against a synthetic Didi-like page.

These tests launch a real Chromium, but only against a local HTTP server with
synthetic orders.  They never touch Didi, the owner's browser profile, Keychain or the public internet.

They are heavy, so they are opt-in:

    PFIN_BROWSER_TESTS=1 python3.14 -m unittest tests.test_didi_browser_offline

Use a Python that has Playwright installed.  The Node variant is also tried
when Node Playwright is installed.
"""

from __future__ import annotations

import json
import shutil
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from connectors.didi.browser import (
    BrowserEngine,
    NodePlaywrightDriver,
    PythonPlaywrightDriver,
    build_browser_payload,
    detect_browser_engine,
    find_installed_chromium,
    summarize_browser,
)

from connectors.kit.env import getenv

ENABLED = getenv("PFIN_BROWSER_TESTS") == "1"
SECRET = "SYNTH_OFFLINE_SECRET"
WSGSIG = "SYNTH_OFFLINE_SIGN"

ORDERS = {
    "o1": {
        "orderId": "o1", "product_name": "快车", "hisstatus": "已完成",
        "setuptimestamp": 1700000000, "fromAddress": "起点A", "toAddress": "终点A",
    },
    "o2": {
        "orderId": "o2", "product_name": "专车", "hisstatus": "已完成",
        "setuptimestamp": 1700100000, "fromAddress": "起点B", "toAddress": "终点B",
    },
    "o3": {
        "orderId": "o3", "product_name": "出租车", "hisstatus": "已完成",
        "setuptimestamp": 1700200000, "fromAddress": "起点C", "toAddress": "终点C",
    },
}

PAGE0 = {
    "errno": 0, "timemode": "c1", "mhavenext": 1, "havenext": 1,
    "order_waiting": [ORDERS["o1"]], "order_done": [ORDERS["o2"]],
}
PAGE1 = {
    "errno": 0, "timemode": "c2", "mhavenext": 0, "havenext": 0,
    "order_waiting": [], "order_done": [ORDERS["o3"]],
}

HTML = """<!doctype html><html><head><meta charset="utf-8"><title>我的订单</title>
<style>body{margin:0;font-family:sans-serif}#scroller{height:110px;overflow-y:auto;border:1px solid #ccc}
.order-item{padding:12px;border-bottom:1px solid #eee;cursor:pointer}.order-desc{font-weight:bold}
#detail{position:fixed;top:0;left:0;right:0;bottom:0;background:#fff;display:none;padding:20px}
</style></head><body>
<div id="app"><div id="scroller"><div id="cards"></div><div id="more"></div></div></div>
<div id="detail"><h1>订单详情</h1><div id="detail-body"></div></div>
<script>
const ORDERS = {{ORDERS}};
let pageNum = 0, timemode = '', loading = false, ended = false;
function card(order) {
  const el = document.createElement('div');
  el.className = 'order-item';
  el.innerHTML = '<div class="order-desc">'+order.product_name+' '+order.hisstatus+'</div>'+
    '<div class="from">'+order.fromAddress+'</div><div class="to">'+order.toAddress+'</div>';
  el.__vue__ = { item: order };
  el.addEventListener('click', () => openDetail(order));
  return el;
}
async function load() {
  if (loading || ended) return; loading = true;
  const q = new URLSearchParams({pagenum:String(pageNum), timemode, token:'{{SECRET}}', datatype:'webapp'});
  const data = await fetch('/passenger/history?'+q.toString()).then(r => r.json());
  if (data.timemode) {
    timemode = data.timemode;
    if (Number(data.mhavenext) === 0) pageNum = 0; else pageNum += 1;
    if (!Number(data.havenext)) ended = true;
    for (const item of [...(data.order_waiting||[]), ...(data.order_done||[])]) {
      document.getElementById('cards').appendChild(card(item));
    }
  } else { ended = true; }
  document.getElementById('more').textContent = ended ? '没有更多' : '加载更多';
  loading = false;
}
async function openDetail(order) {
  location.hash = '#/gulfstream-lite?oid='+encodeURIComponent(order.orderId);
  const d = await fetch('/pOrderDetail?order_id='+encodeURIComponent(order.orderId)+'&token={{SECRET}}').then(r=>r.json());
  const f = await fetch('/pGetRealTimeFeeDetail?order_id='+encodeURIComponent(order.orderId)+'&token={{SECRET}}').then(r=>r.json());
  document.getElementById('detail-body').textContent = '订单号 '+order.orderId+' 实付 '+(d.real_pay&&d.real_pay.total_fee);
  document.getElementById('detail').style.display = 'block';
}
document.getElementById('scroller').addEventListener('scroll', () => {
  const el = document.getElementById('scroller');
  if (el.scrollTop + el.clientHeight >= el.scrollHeight - 20) load();
});
window.addEventListener('hashchange', () => {
  if (!location.hash.startsWith('#/gulfstream-lite')) document.getElementById('detail').style.display = 'none';
});
load();
</script></body></html>""".replace(
    "{{ORDERS}}", json.dumps(ORDERS, ensure_ascii=False)
).replace("{{SECRET}}", SECRET)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, body: bytes, content_type: str = "application/json"):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parts = urlsplit(self.path)
        if parts.path == "/order-list":
            self._send(HTML.encode("utf-8"), "text/html; charset=utf-8")
            return
        if parts.path == "/passenger/history":
            page = int(parse_qs(parts.query).get("pagenum", ["0"])[0])
            self._send(json.dumps(PAGE0 if page == 0 else PAGE1, ensure_ascii=False).encode())
            return
        if parts.path == "/pOrderDetail":
            self._send(json.dumps({
                "errno": 0,
                "real_pay": {"total_fee": "12.30"},
                "token": SECRET,
                "wsgsig": WSGSIG,
            }, ensure_ascii=False).encode())
            return
        if parts.path == "/pGetRealTimeFeeDetail":
            self._send(json.dumps({
                "errno": 0,
                "data": {"real_pay": {"total_fee": "12.30"}},
                "token": SECRET,
                "wsgsig": WSGSIG,
            }, ensure_ascii=False).encode())
            return
        self.send_response(404)
        self.end_headers()


class SyntheticServer:
    def __enter__(self):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/order-list"
        return self

    def __exit__(self, *args):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)


def _assert_capture(result):
    kinds = [capture["kind"] for capture in result["captures"]]
    assert kinds.count("order_list") == 2, kinds
    assert kinds.count("order_detail") == 3, kinds
    assert kinds.count("fare_detail") == 3, kinds
    assert set(result["details"]) == {"o1", "o2", "o3"}
    assert not result["blocked"], result["blocked"]


@unittest.skipUnless(ENABLED, "set PFIN_BROWSER_TESTS=1 to run real Chromium tests")
class OfflineBrowserTests(unittest.TestCase):
    def test_python_driver_scrolls_clicks_and_captures(self):
        try:
            import playwright.sync_api  # noqa: F401
        except Exception:
            self.skipTest("Python Playwright not installed")
        executable = find_installed_chromium()
        if not executable:
            self.skipTest("no installed Playwright Chromium")
        with SyntheticServer() as server:
            driver = PythonPlaywrightDriver(
                BrowserEngine("python", executable),
                timeout=15,
                max_pages=5,
                with_detail=True,
                with_fare=True,
                url=server.url,
            )
            result = driver.run([
                {"name": "ticket", "value": SECRET, "domain": "127.0.0.1",
                 "path": "/", "secure": False, "sameSite": "Lax"}
            ])
        _assert_capture(result)
        payload = build_browser_payload(result, secrets=[SECRET, WSGSIG],
                                        start_url=server.url)
        self.assertEqual(len(payload["trips"]), 3)
        self.assertEqual(summarize_browser(payload)["with_fare"], 3)
        text = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn(SECRET, text)
        self.assertNotIn(WSGSIG, text)

    def test_node_driver_scrolls_clicks_and_captures(self):
        if not shutil.which("node"):
            self.skipTest("node not installed")
        try:
            engine = detect_browser_engine("node")
        except Exception:
            self.skipTest("Node Playwright not installed")
        with SyntheticServer() as server:
            driver = NodePlaywrightDriver(
                engine,
                timeout=15,
                max_pages=5,
                with_detail=True,
                with_fare=True,
                url=server.url,
            )
            result = driver.run([
                {"name": "ticket", "value": SECRET, "domain": "127.0.0.1",
                 "path": "/", "secure": False, "sameSite": "Lax"}
            ])
        _assert_capture(result)
        payload = build_browser_payload(result, secrets=[SECRET, WSGSIG],
                                        start_url=server.url)
        self.assertEqual(len(payload["trips"]), 3)
        text = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn(SECRET, text)
        self.assertNotIn(WSGSIG, text)


if __name__ == "__main__":
    unittest.main()
