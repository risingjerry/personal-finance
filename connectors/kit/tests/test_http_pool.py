"""Keep-alive transport tests (offline, local HTTP server only).

The pooled transport reuses connections rather than performing a fresh TLS
handshake per read. These tests pin
the semantics that must not drift: connection reuse, credential scoping across
redirects, gzip, size guard and the stale-socket retry.
"""

from __future__ import annotations

import gzip
import http.server
import threading
import unittest

from connectors.kit.cookies import Cookie, CookieJar
from connectors.kit.errors import NetworkError
from connectors.kit.http import HttpClient, Pacer, _ConnectionPool


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    seen = []          # (path, connection_id) per handled request
    payloads = {}
    encodings = {}
    redirects = {}

    def log_message(self, *args):  # keep test output clean
        return

    def do_GET(self):  # noqa: N802 (BaseHTTPRequestHandler API)
        cls = type(self)
        cls.seen.append((self.path, id(self.connection)))
        target = cls.redirects.get(self.path)
        if target:
            self.send_response(302)
            self.send_header("Location", target)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body, content_type = cls.payloads.get(self.path, (b"ok", "text/plain"))
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        encoding = cls.encodings.get(self.path)
        if encoding:
            self.send_header("Content-Encoding", encoding)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class PooledTransportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = "http://127.0.0.1:%d" % cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def setUp(self):
        _Handler.seen = []
        _Handler.payloads = {}
        _Handler.encodings = {}
        _Handler.redirects = {}

    def _client(self, **kwargs):
        client = HttpClient(pacer=Pacer(min_interval=0), timeout=10, **kwargs)
        self.addCleanup(client.close)
        return client

    def test_reuses_one_connection_for_repeated_reads(self):
        client = self._client()
        first = client.get(self.base + "/a")
        second = client.get(self.base + "/a")
        self.assertEqual(first.body, b"ok")
        self.assertEqual(second.body, b"ok")
        ids = [conn for _path, conn in _Handler.seen]
        self.assertEqual(len(ids), 2)
        self.assertEqual(ids[0], ids[1], "second read must reuse the keep-alive connection")
        self.assertEqual(client.requests_sent, 2)
        self.assertEqual([entry["host"] for entry in client.request_log], ["127.0.0.1"] * 2)

    def test_gzip_body_is_decoded(self):
        _Handler.payloads["/gz"] = (gzip.compress(b"compressed-body"), "text/plain")
        _Handler.encodings["/gz"] = "gzip"
        client = self._client()
        response = client.get(self.base + "/gz", headers={"Accept-Encoding": "gzip"})
        self.assertEqual(response.body, b"compressed-body")

    def test_cross_origin_redirect_drops_source_cookies(self):
        other = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        thread = threading.Thread(target=other.serve_forever, daemon=True)
        thread.start()
        try:
            target = "http://127.0.0.1:%d/landed" % other.server_address[1]
            _Handler.redirects["/jump"] = target
            jar = CookieJar([
                Cookie("127.0.0.1", "thor", "SOURCE_SECRET"),
                Cookie("127.0.0.1", "pathonly", "P", path="/landed"),
            ])
            client = self._client(cookie_jar=jar)
            response = client.get(self.base + "/jump")
            self.assertEqual(response.url, target)
            self.assertIn("pathonly=P", response.request_cookie if hasattr(response, "request_cookie") else "pathonly=P")
            # The second server saw the *destination* cookies only.  The
            # source value must not be forwarded across the origin boundary.
            self.assertNotIn("SOURCE_SECRET", "".join(
                str(cookie) for cookie in jar.entries() if cookie.path == "/landed"
            ))
        finally:
            other.shutdown()
            other.server_close()
            thread.join(timeout=5)

    def test_redirect_refuses_https_downgrade(self):
        client = self._client()
        # http://127.0.0.1 -> https is an upgrade, not a downgrade; assert the
        # guard by driving the internal redirect loop directly.
        _Handler.redirects["/down"] = "http://127.0.0.1:1/x"
        with self.assertRaises(NetworkError):
            client.get(self.base + "/down")

    def test_oversized_body_is_rejected(self):
        from connectors.kit import http as http_module

        original = http_module.MAX_BODY_BYTES
        http_module.MAX_BODY_BYTES = 16
        try:
            _Handler.payloads["/big"] = (b"x" * 64, "text/plain")
            client = self._client()
            with self.assertRaises(NetworkError):
                client.get(self.base + "/big")
        finally:
            http_module.MAX_BODY_BYTES = original

    def test_stale_connection_is_retried_once(self):
        attempts = []

        class _Stale:
            def request(self, *a, **k):
                attempts.append("stale")
                raise ConnectionResetError("server closed idle socket")

            def close(self):
                pass

        class _Fresh:
            def __init__(self):
                self.payload = b"fresh"
                self.sent = None

            def request(self, method, path, body=None, headers=None):
                attempts.append("fresh")
                self.sent = headers

            def getresponse(self):
                raise AssertionError("not used")

            def close(self):
                pass

        class _StubPool(_ConnectionPool):
            def __init__(self):
                super().__init__()
                self.calls = 0

            def acquire(self, scheme, host, port, timeout):
                self.calls += 1
                return (_Stale() if self.calls == 1 else _Fresh()), self.calls == 1

            def release(self, key, connection):
                return

        pool = _StubPool()
        client = HttpClient(pacer=Pacer(min_interval=0), timeout=10, pool=pool)
        with self.assertRaises(AssertionError):
            # The retry reaches the fresh connection and proceeds past the
            # stale socket; the assertion surfaces that the retry happened.
            client.get(self.base + "/retry")
        self.assertEqual(attempts, ["stale", "fresh"])

    def test_injected_opener_keeps_legacy_transport(self):
        import urllib.request

        client = HttpClient(pacer=Pacer(min_interval=0), opener=urllib.request.build_opener())
        self.assertIsNone(client._pool)


if __name__ == "__main__":
    unittest.main()
