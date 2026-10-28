"""Small HTTP layer with polite pacing and injectable transport."""

from __future__ import annotations

import gzip
import http.client
import json
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Mapping, Optional, Tuple

from .errors import NetworkError

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)
MAX_BODY_BYTES = 32 * 1024 * 1024


class _CookieScopedRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Drop credentials when a redirect leaves the original host.

    ``urllib`` copies request headers into the redirected request.  Without
    this guard an open redirect would forward the Cookie header to another
    host even though a browser would not.
    """

    def __init__(self, client=None):
        self.client = client
        super().__init__()

    max_redirections = 5

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is None:
            return None
        old = urllib.parse.urlsplit(req.full_url)
        new = urllib.parse.urlsplit(newurl)
        if (old.hostname, old.port) != (new.hostname, new.port):
            for name in ("Cookie", "cookie", "Authorization", "authorization"):
                redirected.remove_header(name)
        if self.client:
            jar = self.client.cookie_jar
            if jar is not None:
                jar.apply_response(Response(code, req.full_url, {}, b'', headers.get_all('Set-Cookie') or []))
                # Rebuild from the destination's own cookies, not the source
                # header. .jd.com is NEVER copied into .jd.hk.
                redirected.remove_header('Cookie')
                if new.scheme != 'https' or new.username or new.password:
                    raise NetworkError('拒绝不安全的会话重定向')
                cookie = jar.header_for(new.hostname or '', path=new.path or '/')
                if cookie:
                    redirected.add_unredirected_header('Cookie', cookie)
            self.client.pacer.wait()
        return redirected


class Pacer:
    """Ensure a minimum gap between successive request *starts*.

    Slot reservation instead of sleep-then-record: each caller atomically
    claims the next free start slot and then sleeps only until *its own* slot,
    so workers overlap their network time while the request rate stays capped
    at ``1 / min_interval``.  A single sequential caller behaves exactly as
    before (slot = last start + interval).
    """

    def __init__(
        self,
        min_interval: float = 1.0,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        if min_interval < 0:
            raise ValueError("min_interval must be >= 0")
        self.min_interval = min_interval
        self.clock = clock
        self.sleep = sleep
        self._next_slot: Optional[float] = None
        self._lock = threading.Lock()
        self.waits = 0

    def wait(self) -> None:
        with self._lock:
            now = self.clock()
            slot = self._next_slot if self._next_slot is not None else now
            if slot < now:
                slot = now
            self._next_slot = slot + self.min_interval
            delay = slot - now
        if delay > 0:
            self.sleep(delay)
            self.waits += 1


@dataclass
class Response:
    status: int
    url: str
    headers: Dict[str, str]
    body: bytes
    set_cookies: list = field(default_factory=list)

    @property
    def text(self) -> str:
        for encoding in ("utf-8", "gb18030", "latin-1"):
            try:
                return self.body.decode(encoding)
            except UnicodeDecodeError:
                continue
        return self.body.decode("utf-8", "replace")

    def header(self, name: str, default: str = "") -> str:
        return self.headers.get(name.lower(), default)

    def json(self):
        try:
            return json.loads(self.text)
        except ValueError as exc:
            raise NetworkError("服务器返回的内容不是合法 JSON") from exc


class _ConnectionPool:
    """Keep-alive connection pool for repeated reads against the same host.

    Reusing a connection avoids a fresh TLS handshake for each read.
    Connections are keyed by
    ``(scheme, host, port)`` and a stale idle connection is replaced
    transparently once, because servers close idle keep-alive sockets.
    """

    def __init__(self, max_idle_per_host: int = 4) -> None:
        self._lock = threading.Lock()
        self._idle: Dict[Tuple[str, str, int], List[http.client.HTTPConnection]] = {}
        self.max_idle_per_host = max_idle_per_host
        self.ssl_context = ssl.create_default_context()

    def acquire(
        self, scheme: str, host: str, port: int, timeout: float
    ) -> Tuple[http.client.HTTPConnection, bool]:
        """Return ``(connection, reused)``; a reused connection may be stale."""

        key = (scheme, host, port)
        with self._lock:
            idle = self._idle.get(key)
            if idle:
                return idle.pop(), True
        if scheme == "https":
            return (
                http.client.HTTPSConnection(
                    host, port, timeout=timeout, context=self.ssl_context
                ),
                False,
            )
        return http.client.HTTPConnection(host, port, timeout=timeout), False

    def release(self, key, connection) -> None:
        with self._lock:
            idle = self._idle.setdefault(key, [])
            if len(idle) < self.max_idle_per_host:
                idle.append(connection)
                return
        self.discard(connection)

    @staticmethod
    def discard(connection) -> None:
        try:
            connection.close()
        except OSError:
            pass

    def close(self) -> None:
        with self._lock:
            idle = [c for conns in self._idle.values() for c in conns]
            self._idle.clear()
        for connection in idle:
            self.discard(connection)


class HttpClient:
    def __init__(
        self,
        *,
        pacer: Optional[Pacer] = None,
        timeout: float = 30.0,
        user_agent: str = DEFAULT_USER_AGENT,
        opener=None,
        cookie_jar=None,
        pool: Optional["_ConnectionPool"] = None,
    ):
        self.pacer = pacer or Pacer()
        self.timeout = timeout
        self.user_agent = user_agent
        self.cookie_jar = cookie_jar
        self._opener = opener or urllib.request.build_opener(
            _CookieScopedRedirectHandler(self)
        )
        # A caller-supplied opener keeps full control of the transport (tests
        # and 大众点评's no-redirect opener).  Otherwise reads reuse keep-alive
        # connections, which is 3-5x faster per request on this network.
        self._pool = pool if pool is not None else (None if opener is not None else _ConnectionPool())
        # Observability for benchmarks and per-site summaries: one entry per
        # real network request (redirect hops included).  No URLs with query
        # secrets are kept -- only host, scheme and duration.
        self.requests_sent = 0
        self.request_log: List[Dict[str, object]] = []
        self._record_lock = threading.Lock()

    def _base_headers(self, extra: Optional[Mapping[str, str]] = None) -> Dict[str, str]:
        headers = {
            "User-Agent": self.user_agent,
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Cache-Control": "no-cache",
        }
        if extra:
            headers.update({k: v for k, v in extra.items() if v is not None})
        return headers

    def _open(self, request: urllib.request.Request) -> Response:
        if self.cookie_jar is not None:
            url = urllib.parse.urlsplit(request.full_url)
            request.remove_header('Cookie')
            cookie = self.cookie_jar.header_for(
                url.hostname or '', path=url.path or '/', secure=url.scheme == 'https'
            )
            if cookie:
                request.add_unredirected_header('Cookie', cookie)
        if self._pool is not None:
            return self._open_pooled(request)
        self.pacer.wait()
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                body = response.read(MAX_BODY_BYTES + 1)
                if len(body) > MAX_BODY_BYTES:
                    raise NetworkError("服务器响应过大，已停止读取")
                if (response.headers.get("Content-Encoding") or "").lower() == "gzip":
                    try:
                        body = gzip.decompress(body)
                    except (OSError, EOFError) as exc:
                        raise NetworkError("无法解压服务器响应") from exc
                headers = {
                    key.lower(): value for key, value in response.headers.items()
                }
                set_cookies = response.headers.get_all("Set-Cookie") or []
                return self._finish(Response(
                    response.status, response.geturl(), headers, body, list(set_cookies)
                ))
        except urllib.error.HTTPError as exc:
            body = exc.read(MAX_BODY_BYTES + 1)
            if len(body) > MAX_BODY_BYTES:
                body = b""
            if (exc.headers.get("Content-Encoding") or "").lower() == "gzip":
                try:
                    body = gzip.decompress(body)
                except (OSError, EOFError):
                    body = b""
            headers = {key.lower(): value for key, value in exc.headers.items()}
            set_cookies = exc.headers.get_all("Set-Cookie") or []
            return self._finish(Response(exc.code, exc.geturl(), headers, body, list(set_cookies)))
        except urllib.error.URLError as exc:
            raise NetworkError(f"网络请求失败: {exc.reason}") from exc
        except TimeoutError as exc:
            raise NetworkError("网络请求超时") from exc
        except OSError as exc:
            raise NetworkError(f"网络请求失败: {exc}") from exc

    def _finish(self, response):
        if self.cookie_jar is not None:
            self.cookie_jar.apply_response(response)
        return response

    # ------------------------------------------------------------------
    # pooled transport (keep-alive)
    # ------------------------------------------------------------------

    _REDIRECT_STATUSES = (301, 302, 303, 307, 308)
    _MAX_REDIRECTS = 5

    def _open_pooled(self, request: urllib.request.Request) -> Response:
        """Send *request* over a reused keep-alive connection.

        Semantics deliberately mirror the urllib path: credentials are never
        forwarded across a redirect to another host, HTTPS is never downgraded,
        every hop's ``Set-Cookie`` reaches the jar, and gzip bodies are
        unwrapped.
        """

        url = request.full_url
        method = request.get_method()
        data = request.data
        headers = self._request_headers(request)
        for _hop in range(self._MAX_REDIRECTS + 1):
            parts = urllib.parse.urlsplit(url)
            if parts.scheme not in ("http", "https"):
                raise NetworkError("只支持 http/https 请求")
            if parts.username or parts.password:
                raise NetworkError("拒绝带内嵌凭据的地址")
            if self.cookie_jar is not None and parts.hostname:
                header = self.cookie_jar.header_for(
                    parts.hostname,
                    path=parts.path or "/",
                    secure=parts.scheme == "https",
                )
                if header:
                    headers["Cookie"] = header
                else:
                    headers.pop("Cookie", None)
            status, response_headers, body, connection_key = self._send_once(
                parts, method, headers, data
            )
            set_cookies = response_headers.pop("__set_cookies__", [])
            response = self._finish(
                Response(status, url, response_headers, body, list(set_cookies))
            )
            location = response_headers.get("location")
            if status not in self._REDIRECT_STATUSES or not location:
                return response
            next_url = urllib.parse.urljoin(url, location)
            next_parts = urllib.parse.urlsplit(next_url)
            if parts.scheme == "https" and next_parts.scheme != "https":
                raise NetworkError("拒绝不安全的会话重定向")
            if (parts.hostname, parts.port) != (next_parts.hostname, next_parts.port):
                # Never carry the source host's cookies or auth across hosts;
                # the jar rebuilds them for the destination only.
                headers.pop("Authorization", None)
                headers.pop("authorization", None)
                headers.pop("Cookie", None)
            if status in (301, 302, 303) and method not in ("GET", "HEAD"):
                method = "GET"
                data = None
                headers.pop("Content-Type", None)
                headers.pop("Content-Length", None)
            url = next_url
        raise NetworkError("重定向次数过多，已停止")

    @staticmethod
    def _request_headers(request: urllib.request.Request) -> Dict[str, str]:
        headers: Dict[str, str] = {}
        for source in (request.headers, request.unredirected_hdrs):
            for key, value in (source or {}).items():
                headers[key] = value
        headers.pop("Cookie", None)
        return headers

    def _send_once(self, parts, method, headers, data):
        """One logical request; retries once if a reused socket was stale."""

        host = parts.hostname or ""
        port = parts.port or (443 if parts.scheme == "https" else 80)
        key = (parts.scheme, host, port)
        body = data if isinstance(data, (bytes, bytearray)) else None
        started = time.monotonic()
        connection = None
        reused = False
        try:
            connection, reused = self._pool.acquire(
                parts.scheme, host, port, self.timeout
            )
            return self._exchange(parts, method, headers, body, connection, key)
        except NetworkError:
            if connection is not None:
                self._pool.discard(connection)
            raise
        except (http.client.HTTPException, OSError, TimeoutError) as exc:
            if connection is not None:
                self._pool.discard(connection)
            if reused:
                # A server-closed keep-alive socket surfaces as a send
                # failure; retry once on a fresh connection, never twice.
                connection, _ = self._pool.acquire(
                    parts.scheme, host, port, self.timeout
                )
                try:
                    return self._exchange(parts, method, headers, body, connection, key)
                except NetworkError:
                    self._pool.discard(connection)
                    raise
                except (http.client.HTTPException, OSError, TimeoutError) as exc2:
                    self._pool.discard(connection)
                    raise NetworkError(f"网络请求失败: {type(exc2).__name__}") from exc2
            raise NetworkError(f"网络请求失败: {type(exc).__name__}") from exc

    def _exchange(self, parts, method, headers, body, connection, key):
        host = parts.hostname or ""
        port = parts.port or (443 if parts.scheme == "https" else 80)
        path = urllib.parse.urlunsplit(("", "", parts.path or "/", parts.query, ""))
        send_headers = {k: v for k, v in headers.items()}
        send_headers["Host"] = parts.netloc
        send_headers.setdefault("Accept-Encoding", "gzip")
        send_headers["Connection"] = "keep-alive"
        if body is not None:
            send_headers["Content-Length"] = str(len(body))
        self.pacer.wait()
        started = time.monotonic()
        connection.request(method, path, body=body, headers=send_headers)
        raw = connection.getresponse()
        payload = raw.read(MAX_BODY_BYTES + 1)
        status = raw.status
        response_headers = {k.lower(): v for k, v in raw.getheaders()}
        response_headers["__set_cookies__"] = raw.headers.get_all("Set-Cookie") or []
        will_close = raw.will_close
        if len(payload) > MAX_BODY_BYTES:
            self._pool.discard(connection)
            raise NetworkError("服务器响应过大，已停止读取")
        self._record(parts, started)
        if will_close:
            self._pool.discard(connection)
        else:
            self._pool.release(key, connection)
        if (response_headers.get("content-encoding") or "").lower() == "gzip":
            try:
                payload = gzip.decompress(payload)
            except (OSError, EOFError) as exc:
                raise NetworkError("无法解压服务器响应") from exc
        return status, response_headers, payload, key

    def _record(self, parts, started) -> None:
        entry = {
            "host": parts.hostname,
            "ms": round((time.monotonic() - started) * 1000, 1),
        }
        with self._record_lock:
            self.requests_sent += 1
            self.request_log.append(entry)

    def close(self) -> None:
        if self._pool is not None:
            self._pool.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()
        return False

    def get(
        self,
        url: str,
        *,
        cookie_header: str = "",
        headers: Optional[Mapping[str, str]] = None,
    ) -> Response:
        extra = dict(headers or {})
        if cookie_header:
            extra["Cookie"] = cookie_header
        extra.setdefault("Accept", "text/html,application/json;q=0.9,*/*;q=0.8")
        request = urllib.request.Request(url, headers=self._base_headers(extra), method="GET")
        return self._open(request)

    def post_form(
        self,
        url: str,
        form: Mapping[str, str],
        *,
        cookie_header: str = "",
        headers: Optional[Mapping[str, str]] = None,
    ) -> Response:
        extra = dict(headers or {})
        if cookie_header:
            extra["Cookie"] = cookie_header
        extra.setdefault("Accept", "application/json, text/plain, */*")
        extra.setdefault("Content-Type", "application/x-www-form-urlencoded; charset=UTF-8")
        body = urllib.parse.urlencode(form).encode("utf-8")
        request = urllib.request.Request(
            url, data=body, headers=self._base_headers(extra), method="POST"
        )
        return self._open(request)

    def post_json(
        self,
        url: str,
        payload: object,
        *,
        cookie_header: str = "",
        headers: Optional[Mapping[str, str]] = None,
    ) -> Response:
        """POST a compact JSON body (used by Meituan's bill/userInfo endpoints)."""

        extra = dict(headers or {})
        if cookie_header:
            extra["Cookie"] = cookie_header
        extra.setdefault("Accept", "application/json, text/plain, */*")
        extra.setdefault("Content-Type", "application/json; charset=UTF-8")
        body = json.dumps(
            payload, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        request = urllib.request.Request(
            url, data=body, headers=self._base_headers(extra), method="POST"
        )
        return self._open(request)
