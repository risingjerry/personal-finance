"""Synthetic, offline tests for the Didi reader.

No test reads the real browser cookie database, calls Keychain, or uses network.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from connectors.didi import CONNECTOR, didi_status as _didi_status, fetch_didi
from connectors.didi.blocking import check_didi_html, check_didi_payload
from connectors.kit.cookies import Cookie, CookieJar
from connectors.kit.runner import build_parser as _build_parser, main as _main
from connectors.didi.reader import (
    DIDI_FARE_DETAIL_ENDPOINT,
    DIDI_HISTORY_ENDPOINT,
    DIDI_ORDER_DETAIL_ENDPOINT,
    DIDI_TOKEN_ENV,
    DidiFetcher,
    _find_fare,
    decode_cookie_token,
    find_token_cookie,
    parse_order_item,
    parse_order_list,
    redact_secret,
    token_from_env,
)
from connectors.kit.errors import (
    ApiError,
    AuthRequired,
    NeedsRiskSignature,
    NetworkError,
    RiskBlocked,
)
from connectors.kit.http import Response
from connectors.kit.output import file_mode, write_json

SECRET = "SYNTHETIC_DIDI_TOKEN_DO_NOT_LEAK"


def build_parser():
    class _P:
        @staticmethod
        def parse_args(argv):
            assert argv[0] == "didi"
            return _build_parser(CONNECTOR).parse_args(argv[1:])
    return _P


def main(argv):
    assert argv[0] == "didi"
    return _main(CONNECTOR, argv[1:])


def didi_status(args, *, provider=None):
    return _didi_status(args, provider=provider)[0]


def order(oid="o1", **overrides):
    item = {
        "orderId": oid,
        "productId": 260,
        "product_name": "快车",
        "hisstatus": "已完成",
        "setuptimestamp": 1700000000,
        "fromAddress": "起点",
        "toAddress": "终点",
        "scene": ["快车"],
        "classtype": 1,
        "extra_data": {"note": "synthetic"},
    }
    item.update(overrides)
    return item


def json_response(payload, *, status=200, url=DIDI_HISTORY_ENDPOINT):
    return Response(
        status=status,
        url=url,
        headers={},
        body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
    )


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, *, cookie_header="", headers=None):
        self.calls.append(
            {"url": url, "cookie_header": cookie_header, "headers": dict(headers or {})}
        )
        if not self.responses:
            raise AssertionError("unexpected extra network request")
        return self.responses.pop(0)


def make_fetcher(client, **kwargs):
    return DidiFetcher(
        client,
        SECRET,
        with_detail=kwargs.pop("with_detail", False),
        with_fare=kwargs.pop("with_fare", False),
        **kwargs,
    )


class FakeProvider:
    def __init__(self, jar):
        self.jar = jar
        self.calls = 0

    def load(self):
        self.calls += 1
        return self.jar


class DidiParseTests(unittest.TestCase):
    def test_parse_order_list_merges_waiting_and_done(self):
        payload = {
            "errno": 0,
            "order_waiting": [order("waiting")],
            "order_done": [order("done"), order("waiting")],
        }
        orders = parse_order_list(payload)
        self.assertEqual([item["order_id"] for item in orders], ["waiting", "done"])
        first = orders[0]
        self.assertEqual(first["product_id"], "260")
        self.assertEqual(first["product_name"], "快车")
        self.assertEqual(first["status"], "已完成")
        self.assertEqual(first["from"], "起点")
        self.assertEqual(first["to"], "终点")
        self.assertEqual(first["scene"], ["快车"])
        self.assertEqual(first["occurred"], "2023-11-15 06:13:20")

    def test_timestamp_seconds_and_milliseconds(self):
        seconds = parse_order_item(order("a", setuptimestamp=1700000000))
        millis = parse_order_item(order("b", setuptimestamp=1700000000000))
        self.assertEqual(seconds["occurred"], millis["occurred"])

    def test_find_fare_prefers_real_pay_and_ignores_discount(self):
        detail = {
            "real_pay": {"total_fee": "<span>12.30</span>"},
            "need_pay": {"total_fee": "20.00"},
            "discount": [{"name": "优惠", "value": "7.70"}],
        }
        self.assertEqual(_find_fare(detail), "12.30")
        self.assertEqual(_find_fare({"total_fee": "¥ 9.90"}), "9.90")
        self.assertEqual(
            _find_fare({"discount": [{"value": "5"}], "total_fee": "12.30"}),
            "12.30",
        )

    def test_history_params_are_token_query_and_zero_based(self):
        from connectors.didi.reader import build_history_params

        params = build_history_params(
            0, "", SECRET, phone="13800000000", openid="open", channel="1"
        )
        self.assertEqual(params["pagenum"], "0")
        self.assertEqual(params["timemode"], "")
        self.assertEqual(params["datatype"], "webapp")
        self.assertEqual(params["apiver"], "2.0.0")
        self.assertEqual(params["token"], SECRET)
        self.assertEqual(params["phone"], "13800000000")
        self.assertEqual(params["openid"], "open")

    def test_detail_and_fare_params_include_order_and_token(self):
        from connectors.didi.reader import build_detail_params, build_fare_params

        item = order("42", productId=258)
        detail = build_detail_params(item, SECRET)
        fare = build_fare_params(item, SECRET)
        for params in (detail, fare):
            self.assertEqual(params["token"], SECRET)
            self.assertEqual(params["order_id"], "42")
        self.assertEqual(detail["business_id"], "258")
        self.assertEqual(fare["business_id"], "258")

    def test_redact_secret_removes_key_value_and_url_query(self):
        payload = {
            "token": SECRET,
            "nested": {"note": f"prefix {SECRET} suffix"},
            "url": f"https://x/y?token={SECRET}&keep=1",
            "list": [SECRET],
        }
        cleaned = redact_secret(payload, SECRET)
        text = json.dumps(cleaned, ensure_ascii=False)
        self.assertNotIn(SECRET, text)
        self.assertEqual(cleaned["token"], "[REDACTED]")
        self.assertIn("token=[REDACTED]", cleaned["url"])
        self.assertIn("keep=1", cleaned["url"])


class DidiAuthTests(unittest.TestCase):
    def test_missing_token_is_auth_required_and_names_env(self):
        with self.assertRaises(AuthRequired) as caught:
            token_from_env({})
        self.assertIn(DIDI_TOKEN_ENV, caught.exception.message)

    def test_token_from_env_is_stripped(self):
        self.assertEqual(token_from_env({DIDI_TOKEN_ENV: "  abc  "}), "abc")


class DidiCookieAuthTests(unittest.TestCase):
    def test_find_token_cookie_prefers_plain_ticket(self):
        jar = CookieJar(
            [
                Cookie(".diditaxi.com.cn", "didih5_trinity_login_ticket_role_30004", "sdk"),
                Cookie("common.diditaxi.com.cn", "ticket", SECRET),
                Cookie(".diditaxi.com.cn", "openid", "OPENID"),
            ]
        )
        found = find_token_cookie(jar)
        self.assertIsNotNone(found)
        self.assertEqual(found.name, "ticket")
        self.assertEqual(found.host_key, "common.diditaxi.com.cn")
        self.assertEqual(found.value, SECRET)
        self.assertNotIn(SECRET, repr(found))

    def test_find_token_cookie_generic_ticket_fallback(self):
        jar = CookieJar([Cookie(".xiaojukeji.com", "my_ticket", "abc")])
        found = find_token_cookie(jar)
        self.assertIsNotNone(found)
        self.assertEqual(found.name, "my_ticket")
        self.assertEqual(found.value, "abc")

    def test_decode_cookie_token_undoes_uri_encoding(self):
        self.assertEqual(decode_cookie_token("a%2Bb%2Fc"), "a+b/c")
        self.assertEqual(decode_cookie_token("plain-token"), "plain-token")

    def test_fetch_uses_cookie_when_env_is_missing(self):
        jar = CookieJar(
            [
                Cookie("common.diditaxi.com.cn", "ticket", SECRET),
                Cookie(".diditaxi.com.cn", "openid", "OPENID"),
            ]
        )
        provider = FakeProvider(jar)
        args = build_parser().parse_args(
            ["didi", "fetch", "--out", "OUT", "--no-detail", "--no-fare"]
        )
        client = FakeClient(
            [
                json_response({"errno": 0, "order_done": [order("a")]}),
                json_response({"errno": 0, "order_waiting": [], "order_done": []}),
            ]
        )
        payload, warnings, needs = fetch_didi(
            args, client=client, environ={}, provider=provider
        )
        self.assertFalse(needs)
        self.assertEqual(warnings, [])
        self.assertEqual(payload["token_source"], "cookie:ticket")
        self.assertEqual(provider.calls, 1)
        self.assertIn(f"token={SECRET}", client.calls[0]["url"])
        self.assertIn("ticket=" + SECRET, client.calls[0]["cookie_header"])
        self.assertIn("openid=OPENID", client.calls[0]["cookie_header"])
        self.assertNotIn(SECRET, json.dumps(payload, ensure_ascii=False))

    def test_env_token_wins_without_reading_cookie_db(self):
        provider = FakeProvider(CookieJar([Cookie("common.diditaxi.com.cn", "ticket", "old")]))
        args = build_parser().parse_args(
            ["didi", "fetch", "--out", "OUT", "--no-detail", "--no-fare"]
        )
        client = FakeClient(
            [
                json_response({"errno": 0, "order_done": [order("a")]}),
                json_response({"errno": 0, "order_waiting": [], "order_done": []}),
            ]
        )
        payload, _, _ = fetch_didi(
            args,
            client=client,
            environ={DIDI_TOKEN_ENV: SECRET},
            provider=provider,
        )
        self.assertEqual(provider.calls, 0)
        self.assertEqual(payload["token_source"], "env")
        self.assertEqual(client.calls[0]["cookie_header"], "")

    def test_no_cookie_and_no_env_is_auth_required(self):
        args = build_parser().parse_args(
            ["didi", "fetch", "--out", "OUT", "--no-cookie"]
        )
        with self.assertRaises(AuthRequired) as caught:
            fetch_didi(args, client=FakeClient([]), environ={})
        self.assertIn(DIDI_TOKEN_ENV, caught.exception.message)

    def test_no_token_cookie_is_auth_required(self):
        jar = CookieJar([Cookie(".diditaxi.com.cn", "openid", "OPENID")])
        args = build_parser().parse_args(
            ["didi", "fetch", "--out", "OUT", "--no-detail", "--no-fare"]
        )
        with self.assertRaises(AuthRequired) as caught:
            fetch_didi(
                args,
                client=FakeClient([]),
                environ={},
                provider=FakeProvider(jar),
            )
        self.assertIn("Cookie", caught.exception.message)

    def test_status_probe_redacts_token_and_url(self):
        jar = CookieJar([Cookie("common.diditaxi.com.cn", "ticket", SECRET)])
        args = build_parser().parse_args(["didi", "status", "--probe"])

        class FakeHttp:
            def __init__(self, *args, **kwargs):
                pass

            def get(self, url, **kwargs):
                body = (
                    '{"errno":9,"errmsg":"bad token ' + SECRET + '"}'
                ).encode("utf-8")
                return Response(200, url, {"content-type": "application/json"}, body)

        with mock.patch.dict(os.environ, {}, clear=True), mock.patch(
            "connectors.didi.HttpClient", FakeHttp
        ):
            lines = didi_status(args, provider=FakeProvider(jar))
        text = "\n".join(lines)
        self.assertIn("probe http: 200", text)
        self.assertIn("probe auth source: cookie:ticket", text)
        self.assertNotIn(SECRET, text)
        self.assertNotIn("token=" + SECRET, text)

    def test_status_lists_names_but_never_values(self):
        jar = CookieJar(
            [
                Cookie("common.diditaxi.com.cn", "ticket", SECRET),
                Cookie(".diditaxi.com.cn", "openid", "OPENID"),
            ]
        )
        args = build_parser().parse_args(["didi", "status"])
        with mock.patch.dict(os.environ, {}, clear=True):
            lines = didi_status(args, provider=FakeProvider(jar))
        text = "\n".join(lines)
        self.assertIn("common.diditaxi.com.cn", text)
        self.assertIn("ticket", text)
        self.assertIn("ready: yes", text)
        self.assertNotIn(SECRET, text)
        self.assertNotIn("OPENID", text)


class DidiFetchTests(unittest.TestCase):
    def test_legacy_paging_stops_on_empty_page(self):
        client = FakeClient(
            [
                json_response({"errno": 0, "order_waiting": [], "order_done": [order("a")]}),
                json_response({"errno": 0, "order_waiting": [], "order_done": [order("b")]}),
                json_response({"errno": 0, "order_waiting": [], "order_done": []}),
            ]
        )
        fetcher = make_fetcher(client, max_pages=5)
        payload = fetcher.fetch()
        self.assertEqual(payload["kind"], "didi_trips")
        self.assertEqual(len(payload["pages"]), 3)
        self.assertEqual([item["order_id"] for item in fetcher.orders], ["a", "b"])
        self.assertEqual(len(client.calls), 3)
        self.assertEqual(
            [call["url"].split("pagenum=")[1].split("&")[0] for call in client.calls],
            ["0", "1", "2"],
        )
        self.assertTrue(all(call["cookie_header"] == "" for call in client.calls))
        self.assertNotIn("Cookie", client.calls[0]["headers"])

    def test_legacy_repeated_page_stops(self):
        client = FakeClient(
            [
                json_response({"errno": 0, "order_done": [order("a")]}),
                json_response({"errno": 0, "order_done": [order("a")]}),
            ]
        )
        fetcher = make_fetcher(client, max_pages=5)
        fetcher.fetch()
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(len(fetcher.orders), 1)

    def test_timemode_cursor_paging(self):
        client = FakeClient(
            [
                json_response(
                    {
                        "errno": 0,
                        "timemode": "c1",
                        "mhavenext": 1,
                        "havenext": 1,
                        "order_done": [order("a")],
                    }
                ),
                json_response(
                    {
                        "errno": 0,
                        "timemode": "c2",
                        "mhavenext": 0,
                        "havenext": 0,
                        "order_done": [order("b")],
                    }
                ),
            ]
        )
        fetcher = make_fetcher(client, max_pages=5)
        payload = fetcher.fetch()
        self.assertEqual(len(payload["pages"]), 2)
        self.assertEqual([item["order_id"] for item in fetcher.orders], ["a", "b"])
        self.assertIn("timemode=c1", client.calls[1]["url"])
        self.assertIn("pagenum=1", client.calls[1]["url"])

    def test_timemode_resets_page_when_mhavenext_zero(self):
        client = FakeClient(
            [
                json_response(
                    {
                        "errno": 0,
                        "timemode": "c1",
                        "mhavenext": 0,
                        "havenext": 1,
                        "order_done": [order("a")],
                    }
                ),
                json_response(
                    {
                        "errno": 0,
                        "timemode": "c2",
                        "mhavenext": 0,
                        "havenext": 0,
                        "order_done": [order("b")],
                    }
                ),
            ]
        )
        make_fetcher(client, max_pages=5).fetch()
        self.assertIn("timemode=c1", client.calls[1]["url"])
        self.assertIn("pagenum=0", client.calls[1]["url"])

    def test_max_pages_warning(self):
        client = FakeClient(
            [
                json_response({"errno": 0, "order_done": [order("a")]}),
                json_response({"errno": 0, "order_done": [order("b")]}),
            ]
        )
        fetcher = make_fetcher(client, max_pages=2)
        fetcher.fetch()
        self.assertEqual(len(client.calls), 2)
        self.assertTrue(any("最大页数" in warning for warning in fetcher.warnings))

    def test_list_signature_error_mentions_needs_risk_signature(self):
        client = FakeClient(
            [json_response({"errno": 1001, "errmsg": f"missing wsgsig for {SECRET}"})]
        )
        with self.assertRaises(NeedsRiskSignature) as caught:
            make_fetcher(client).fetch()
        self.assertIn("needs risk signature", caught.exception.message.lower())
        self.assertNotIn(SECRET, caught.exception.render())
        self.assertNotIn(SECRET, str(caught.exception))

    def test_list_unsigned_501_needs_signature(self):
        client = FakeClient(
            [
                Response(
                    status=501,
                    url=DIDI_HISTORY_ENDPOINT,
                    headers={"content-type": "text/html"},
                    body=(
                        b"<html><head><title>501 Not Implemented</title></head>"
                        b"<body><center><h1>501 Not Implemented</h1></center>"
                        b"<hr><center>router/2.43.2</center></body></html>"
                    ),
                )
            ]
        )
        with self.assertRaises(NeedsRiskSignature):
            make_fetcher(client).fetch()

    def test_list_login_error_is_auth_required(self):
        client = FakeClient([json_response({"errno": 101, "errmsg": "login required"})])
        with self.assertRaises(AuthRequired):
            make_fetcher(client).fetch()

    def test_detail_fare_taken_from_order_detail(self):
        client = FakeClient(
            [
                json_response({"errno": 0, "order_done": [order("a")]}),
                json_response({"errno": 0, "order_waiting": [], "order_done": []}),
                json_response(
                    {
                        "errno": 0,
                        "basic_data": {"order_info": {"total_fee": "12.30"}},
                    },
                    url=DIDI_ORDER_DETAIL_ENDPOINT,
                ),
            ]
        )
        fetcher = make_fetcher(client, with_detail=True, with_fare=True)
        payload = fetcher.fetch()
        self.assertEqual(len(client.calls), 3)
        trip = payload["pages"][0]["orders"][0]
        self.assertEqual(trip["fare"], "12.30")
        self.assertIn("detail", trip)

    def test_fare_fallback_endpoint(self):
        client = FakeClient(
            [
                json_response({"errno": 0, "order_done": [order("a")]}),
                json_response({"errno": 0, "order_waiting": [], "order_done": []}),
                json_response(
                    {"errno": 0, "basic_data": {"order_info": {}}},
                    url=DIDI_ORDER_DETAIL_ENDPOINT,
                ),
                json_response(
                    {
                        "errno": 0,
                        "data": {
                            "items": [{"real_pay": {"total_fee": "8.88"}}],
                        },
                    },
                    url=DIDI_FARE_DETAIL_ENDPOINT,
                ),
            ]
        )
        fetcher = make_fetcher(client, with_detail=True, with_fare=True)
        payload = fetcher.fetch()
        self.assertEqual(len(client.calls), 4)
        trip = payload["pages"][0]["orders"][0]
        self.assertEqual(trip["fare"], "8.88")
        self.assertIn("fare_detail", trip)

    def test_detail_signature_stops_and_keeps_list(self):
        client = FakeClient(
            [
                json_response({"errno": 0, "order_done": [order("a")]}),
                json_response({"errno": 0, "order_waiting": [], "order_done": []}),
                json_response(
                    {"errno": 9, "errmsg": "wsgsig invalid"},
                    url=DIDI_ORDER_DETAIL_ENDPOINT,
                ),
            ]
        )
        fetcher = make_fetcher(client, with_detail=True, with_fare=True)
        payload = fetcher.fetch()
        self.assertTrue(fetcher.needs_risk_signature)
        self.assertTrue(payload["risk_signature_required"])
        self.assertEqual(len(payload["pages"]), 2)
        self.assertEqual(len(client.calls), 3)
        self.assertTrue(any("wsgsig" in warning for warning in fetcher.warnings))
        self.assertNotIn("detail", payload["pages"][0]["orders"][0])

    def test_detail_generic_risk_raises(self):
        client = FakeClient(
            [
                json_response({"errno": 0, "order_done": [order("a")]}),
                json_response({"errno": 0, "order_waiting": [], "order_done": []}),
                json_response(
                    {"errno": 9, "errmsg": "请完成验证码"},
                    url=DIDI_ORDER_DETAIL_ENDPOINT,
                ),
            ]
        )
        with self.assertRaises(RiskBlocked):
            make_fetcher(client, with_detail=True, with_fare=True).fetch()

    def test_only_readonly_get_endpoints_are_called(self):
        client = FakeClient(
            [
                json_response({"errno": 0, "order_done": [order("a")]}),
                json_response({"errno": 0, "order_waiting": [], "order_done": []}),
                json_response(
                    {"errno": 0, "basic_data": {"order_info": {"total_fee": "1.00"}}},
                    url=DIDI_ORDER_DETAIL_ENDPOINT,
                ),
            ]
        )
        make_fetcher(client, with_detail=True, with_fare=True).fetch()
        allowed = {
            DIDI_HISTORY_ENDPOINT,
            DIDI_ORDER_DETAIL_ENDPOINT,
            DIDI_FARE_DETAIL_ENDPOINT,
        }
        for call in client.calls:
            base = call["url"].split("?", 1)[0]
            self.assertIn(base, allowed)

    def test_api_error_scrubs_token(self):
        client = FakeClient(
            [json_response({"errno": 1, "errmsg": f"bad token {SECRET}"})]
        )
        with self.assertRaises(ApiError) as caught:
            make_fetcher(client).fetch()
        self.assertNotIn(SECRET, caught.exception.render())
        self.assertNotIn(SECRET, str(caught.exception))

    def test_network_error_scrubs_token(self):
        class FailingClient:
            def get(self, url, *, cookie_header="", headers=None):
                raise NetworkError(f"network failed for {SECRET}")

        with self.assertRaises(NetworkError) as caught:
            DidiFetcher(
                FailingClient(), SECRET, with_detail=False, with_fare=False
            ).fetch()
        self.assertNotIn(SECRET, caught.exception.render())
        self.assertNotIn(SECRET, str(caught.exception))


class DidiBlockingTests(unittest.TestCase):
    def test_signature_payload(self):
        with self.assertRaises(NeedsRiskSignature) as caught:
            check_didi_payload({"errno": 9, "errmsg": "missing wsgsig"})
        self.assertIn("needs risk signature", caught.exception.message.lower())

    def test_auth_payload(self):
        with self.assertRaises(AuthRequired):
            check_didi_payload({"errno": 101, "errmsg": "login_outdate"})

    def test_signature_wins_over_generic_forbidden(self):
        with self.assertRaises(NeedsRiskSignature):
            check_didi_payload({"errno": 403, "errmsg": "wsgsig invalid"})

    def test_risk_payload(self):
        with self.assertRaises(RiskBlocked):
            check_didi_payload({"errno": 9, "errmsg": "安全验证"})

    def test_success_payload(self):
        check_didi_payload({"errno": 0, "order_waiting": [], "order_done": []})

    def test_unsigned_501_is_signature_error(self):
        html = (
            "<html><head><title>501 Not Implemented</title></head>"
            "<body><center><h1>501 Not Implemented</h1></center>"
            "<hr><center>router/2.43.2</center></body></html>"
        )
        with self.assertRaises(NeedsRiskSignature) as caught:
            check_didi_html(html, DIDI_HISTORY_ENDPOINT, 501)
        self.assertIn("risk signature", caught.exception.message.lower())


class DidiCliTests(unittest.TestCase):
    def test_dry_run_does_not_print_token_or_touch_network(self):
        with mock.patch.dict(os.environ, {DIDI_TOKEN_ENV: SECRET}), \
             mock.patch("urllib.request.urlopen", side_effect=AssertionError("network")), \
             mock.patch("subprocess.run", side_effect=AssertionError("keychain")):
            output = io.StringIO()
            errors = io.StringIO()
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                code = main(["didi", "fetch", "--dry-run"])
        self.assertEqual(code, 0)
        text = output.getvalue()
        self.assertIn("/passenger/history", text)
        self.assertIn("wsgsig", text)
        self.assertIn(DIDI_TOKEN_ENV, text)
        self.assertNotIn(SECRET, text + errors.getvalue())

    def test_missing_token_is_auth_error(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            errors = io.StringIO()
            with tempfile.TemporaryDirectory() as temp:
                with contextlib.redirect_stderr(errors):
                    code = main(["didi", "fetch", "--out", temp, "--no-cookie"])
                self.assertEqual(code, 3)
                self.assertEqual(list(Path(temp).glob("didi-trips-*.json")), [])
        self.assertIn(DIDI_TOKEN_ENV, errors.getvalue())

    def test_writes_0600_and_kind(self):
        payload = {
            "kind": "didi_trips",
            "pages": [
                {
                    "pagenum": 0,
                    "timemode": "",
                    "orders": [
                        {"order_id": "a", "occurred": "2024-01-01 00:00:00", "fare": "1.00"}
                    ],
                    "response": {},
                }
            ],
        }
        with tempfile.TemporaryDirectory() as temp:
            with mock.patch(
                "connectors.didi.fetch_didi", return_value=(payload, [], False)
            ):
                output = io.StringIO()
                with contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
                    code = main(["didi", "fetch", "--out", temp])
            self.assertEqual(code, 0)
            files = list(Path(temp).glob("didi-trips-*.json"))
            self.assertEqual(len(files), 1)
            self.assertEqual(file_mode(files[0]), 0o600)
            saved = json.loads(files[0].read_text(encoding="utf-8"))
            self.assertEqual(saved["kind"], "didi_trips")
            self.assertIn("pages=1", output.getvalue())
            self.assertIn("orders=1", output.getvalue())

    def test_signature_required_writes_list_then_exits_4(self):
        payload = {
            "kind": "didi_trips",
            "risk_signature_required": True,
            "pages": [{"pagenum": 0, "timemode": "", "orders": [], "response": {}}],
        }
        with tempfile.TemporaryDirectory() as temp:
            with mock.patch(
                "connectors.didi.fetch_didi", return_value=(payload, ["wsgsig blocked"], True)
            ):
                output = io.StringIO()
                errors = io.StringIO()
                with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                    code = main(["didi", "fetch", "--out", temp])
            self.assertEqual(code, 4)
            files = list(Path(temp).glob("didi-trips-*.json"))
            self.assertEqual(len(files), 1)
            self.assertIn("needs risk signature", errors.getvalue().lower())
            self.assertIn("wsgsig blocked", errors.getvalue())
            self.assertIn("pages=1", output.getvalue())

    def test_real_fetcher_never_writes_token(self):
        args = build_parser().parse_args(
            ["didi", "fetch", "--out", "OUT", "--no-detail", "--no-fare"]
        )
        client = FakeClient(
            [
                json_response(
                    {
                        "errno": 0,
                        "token": SECRET,
                        "order_done": [
                            order("a", url=f"https://x/y?token={SECRET}&a=1")
                        ],
                    }
                ),
                json_response({"errno": 0, "order_waiting": [], "order_done": []}),
            ]
        )
        payload, warnings, needs = fetch_didi(
            args, client=client, environ={DIDI_TOKEN_ENV: SECRET}
        )
        self.assertEqual(warnings, [])
        self.assertFalse(needs)
        with tempfile.TemporaryDirectory() as temp:
            path = write_json(Path(temp), "didi-trips", payload)
            text = path.read_text(encoding="utf-8")
            self.assertNotIn(SECRET, text)
            self.assertIn("didi_trips", text)

    def test_didi_path_does_not_load_cookie_provider(self):
        args = build_parser().parse_args(
            ["didi", "fetch", "--out", "OUT", "--no-detail", "--no-fare"]
        )
        client = FakeClient(
            [
                json_response({"errno": 0, "order_done": [order("a")]}),
                json_response({"errno": 0, "order_waiting": [], "order_done": []}),
            ]
        )
        with mock.patch(
            "connectors.kit.cookies.CookieProvider.load",
            side_effect=AssertionError("must not read cookie db"),
        ):
            fetch_didi(args, client=client, environ={DIDI_TOKEN_ENV: SECRET})


if __name__ == "__main__":
    unittest.main()


def setUpModule():
    from connectors.kit.testing import mock_sessions
    mock_sessions()
