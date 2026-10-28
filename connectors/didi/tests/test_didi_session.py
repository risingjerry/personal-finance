"""Tests for the owner-owned Didi session store (no network, no Keychain)."""

import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from connectors.didi import session as didi_session


GOOD_URL = (
    "https://common.diditaxi.com.cn/general/webEntry?h=1#/order-list"
    "?token=abc123&phone=13800000000"
)


class TempState(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = os.environ.get("PFIN_DIDI_STATE_DIR")
        os.environ["PFIN_DIDI_STATE_DIR"] = self.tmp.name

    def tearDown(self):
        if self.old is None:
            os.environ.pop("PFIN_DIDI_STATE_DIR", None)
        else:
            os.environ["PFIN_DIDI_STATE_DIR"] = self.old
        self.tmp.cleanup()


class EntryUrlTests(TempState):
    def test_accepts_order_list_url_only(self):
        self.assertEqual(didi_session.validate_entry_url(GOOD_URL), GOOD_URL)
        for bad in (
            "http://common.diditaxi.com.cn/general/webEntry#/order-list?token=x",
            "https://evil.example.com/general/webEntry#/order-list?token=x",
            "https://common.diditaxi.com.cn/other#/order-list?token=x",
            "https://common.diditaxi.com.cn/general/webEntry#/home?token=x",
        ):
            with self.assertRaises(ValueError):
                didi_session.validate_entry_url(bad)

    def test_entry_file_roundtrip_is_0600(self):
        path = didi_session.save_entry_url(GOOD_URL)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(didi_session.load_entry_url(), GOOD_URL)

    def test_world_readable_file_is_refused(self):
        path = didi_session.save_entry_url(GOOD_URL)
        os.chmod(path, 0o644)
        self.assertIsNone(didi_session.load_entry_url())

    def test_history_picks_newest_url_with_token(self):
        db = Path(self.tmp.name) / "History"
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE urls (url TEXT, last_visit_time INTEGER)")
        conn.executemany(
            "INSERT INTO urls VALUES (?, ?)",
            [
                ("https://common.diditaxi.com.cn/general/webEntry?h=1#/order-list", 1),
                (GOOD_URL, 2),
                ("https://common.diditaxi.com.cn/general/webEntry?h=1"
                 "#/order-list?token=older&phone=1", 1),
            ],
        )
        conn.commit()
        conn.close()
        self.assertEqual(didi_session.latest_browser_entry_url(db), GOOD_URL)

    def test_history_returns_none_without_history(self):
        self.assertIsNone(
            didi_session.latest_browser_entry_url(Path(self.tmp.name) / "missing")
        )


class CookieTests(TempState):
    def test_clean_cookies_keeps_didi_only(self):
        cookies = [
            {"name": "ticket", "value": "t", "domain": ".diditaxi.com.cn",
             "path": "/", "expires": 1, "httpOnly": True, "secure": True,
             "sameSite": "Lax"},
            {"name": "x", "value": "1", "domain": ".example.com", "path": "/"},
            {"name": "", "value": "1", "domain": ".diditaxi.com.cn"},
        ]
        cleaned = didi_session.clean_cookies(cookies)
        self.assertEqual([c["name"] for c in cleaned], ["ticket"])
        self.assertTrue(cleaned[0]["httpOnly"])

    def test_merge_primary_wins_and_fills_gaps(self):
        primary = [{"name": "ticket", "value": "new", "domain": ".diditaxi.com.cn"}]
        secondary = [
            {"name": "ticket", "value": "old", "domain": ".diditaxi.com.cn"},
            {"name": "openid", "value": "o", "domain": ".diditaxi.com.cn"},
        ]
        merged = didi_session.merge_cookies(primary, secondary)
        values = {c["name"]: c["value"] for c in merged}
        self.assertEqual(values, {"ticket": "new", "openid": "o"})

    def test_session_usable_respects_expiry(self):
        self.assertFalse(didi_session.session_usable(None))
        self.assertFalse(didi_session.session_usable({"cookies": []}))
        expired = {"cookies": [{"name": "ticket", "value": "x", "expires": 100}]}
        self.assertFalse(didi_session.session_usable(expired, now=200))
        alive = {"cookies": [{"name": "ticket", "value": "x", "expires": 300}]}
        self.assertTrue(didi_session.session_usable(alive, now=200))
        session_cookie = {"cookies": [{"name": "ticket", "value": "x", "expires": -1}]}
        self.assertTrue(didi_session.session_usable(session_cookie, now=200))

    def test_session_roundtrip_and_summary(self):
        path = didi_session.save_session(
            [
                {"name": "ticket", "value": "secret-value", "domain": ".diditaxi.com.cn",
                 "expires": 9999999999},
            ],
            entry_url=GOOD_URL,
        )
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        summary = didi_session.session_summary()
        self.assertTrue(summary["stored"])
        self.assertEqual(summary["cookies"], 1)
        self.assertEqual(summary["names"], ["ticket"])
        self.assertTrue(summary["entry_url"])
        self.assertNotIn("secret-value", json.dumps(summary))


class FareCacheTests(TempState):
    def test_cache_roundtrip(self):
        path = didi_session.save_fare_cache({"a": "13.9", "b": ""})
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(didi_session.load_fare_cache(), {"a": "13.9"})

    def test_broken_cache_is_ignored(self):
        didi_session.write_private_text(didi_session.fares_cache_path(), "{not json")
        self.assertEqual(didi_session.load_fare_cache(), {})


class ProfileTests(TempState):
    def test_profile_becomes_ready_after_marker(self):
        self.assertFalse(didi_session.profile_ready())
        didi_session.mark_profile_ready()
        self.assertTrue(didi_session.profile_ready())
        self.assertTrue(didi_session.profile_dir().is_dir())


if __name__ == "__main__":
    unittest.main()
