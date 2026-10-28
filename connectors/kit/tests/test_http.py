import unittest

from connectors.kit.http import Pacer, Response


class FakeClock:
    def __init__(self):
        self.now = 100.0
        self.sleeps = []

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class PacerTests(unittest.TestCase):
    def test_first_request_does_not_sleep(self):
        clock = FakeClock()
        pacer = Pacer(1.0, clock=clock.clock, sleep=clock.sleep)
        pacer.wait()
        self.assertEqual(clock.sleeps, [])
        pacer.wait()
        self.assertEqual(len(clock.sleeps), 1)
        self.assertAlmostEqual(clock.sleeps[0], 1.0)

    def test_long_request_needs_no_extra_sleep(self):
        clock = FakeClock()
        pacer = Pacer(1.0, clock=clock.clock, sleep=clock.sleep)
        pacer.wait()
        clock.now += 2.0
        pacer.wait()
        self.assertEqual(clock.sleeps, [])

    def test_fractional_gap(self):
        clock = FakeClock()
        pacer = Pacer(1.0, clock=clock.clock, sleep=clock.sleep)
        pacer.wait()
        clock.now += 0.25
        pacer.wait()
        self.assertAlmostEqual(clock.sleeps[0], 0.75)


class ResponseTests(unittest.TestCase):
    def test_text_falls_back_to_gb18030(self):
        response = Response(200, "https://x", {}, "中文".encode("gb18030"))
        self.assertEqual(response.text, "中文")

    def test_json(self):
        response = Response(200, "https://x", {}, b'{"a": 1}')
        self.assertEqual(response.json(), {"a": 1})


class _FakeHeaders:
    def items(self):
        return []

    def get(self, name, default=None):
        return default

    def get_all(self, name):
        return []


class _FakeResponse:
    status = 200

    def __init__(self):
        self.headers = _FakeHeaders()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self, limit):
        return b'{"ok":true}'

    def geturl(self):
        return "https://example.test/api"


class _FakeOpener:
    def __init__(self):
        self.request = None
        self.timeout = None

    def open(self, request, timeout):
        self.request = request
        self.timeout = timeout
        return _FakeResponse()


class JsonPostTests(unittest.TestCase):
    def test_post_json_sets_body_and_content_type(self):
        import json

        from connectors.kit.http import HttpClient

        opener = _FakeOpener()
        client = HttpClient(opener=opener, pacer=Pacer(0))
        response = client.post_json(
            "https://example.test/api",
            {"head": {"auth": "secret"}, "Count": 1},
            cookie_header="cticket=secret",
        )
        self.assertEqual(response.status, 200)
        self.assertEqual(opener.request.get_method(), "POST")
        self.assertEqual(
            json.loads(opener.request.data.decode("utf-8")),
            {"head": {"auth": "secret"}, "Count": 1},
        )
        self.assertTrue(
            opener.request.get_header("Content-type").startswith("application/json")
        )
        self.assertEqual(
            opener.request.get_header("Cookie"), "cticket=secret"
        )


if __name__ == "__main__":
    unittest.main()
