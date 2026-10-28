import tempfile
import unittest
from pathlib import Path
from unittest import mock

from connectors.kit.cookies import (
    Cookie,
    CookieJar,
    CookieProvider,
    decrypt_v10_value,
    derive_key,
)
from connectors.kit.errors import CookieError

from connectors.kit.testing import PASSWORD, make_cookie_db


class CookieProviderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "Cookies"
        make_cookie_db(
            self.db,
            [
                (".taobao.com", "_m_h5_tk", "secret-token_123", b"v10"),
                (".taobao.com", "cookie2", "secret-cookie2", b"v10"),
                (".jd.com", "pt_key", "secret-jd-key", b"v10"),
            ],
        )

    def tearDown(self):
        self.temp.cleanup()

    def provider(self, **kwargs):
        return CookieProvider(
            self.db,
            password=PASSWORD,
            host_suffixes=("taobao.com",),
            **kwargs,
        )

    def test_decrypts_v10_with_host_prefix(self):
        jar = self.provider().load()
        self.assertEqual(jar.get("_m_h5_tk", "h5api.m.taobao.com"), "secret-token_123")
        header = jar.header_for("h5api.m.taobao.com")
        self.assertIn("_m_h5_tk=secret-token_123", header)
        self.assertNotIn("pt_key", header)

    def test_repr_never_contains_values(self):
        jar = self.provider().load()
        self.assertNotIn("secret-token_123", repr(jar))
        self.assertNotIn("secret-token_123", repr(jar._cookies[0]))

    def test_plain_value_is_used_when_not_encrypted(self):
        db = Path(self.temp.name) / "plain.db"
        make_cookie_db(
            db,
            [(".taobao.com", "plain_cookie", b"", b"")],
        )
        # The helper's plain path expects a real value, so update it directly.
        import sqlite3

        connection = sqlite3.connect(db)
        connection.execute(
            "UPDATE cookies SET value='plain-secret' WHERE name='plain_cookie'"
        )
        connection.commit()
        connection.close()
        jar = CookieProvider(db, password=PASSWORD, host_suffixes=("taobao.com",)).load()
        self.assertEqual(jar.get("plain_cookie", "h5api.m.taobao.com"), "plain-secret")

    def test_legacy_value_without_host_prefix(self):
        db = Path(self.temp.name) / "legacy.db"
        make_cookie_db(
            db,
            [(".taobao.com", "_m_h5_tk", "legacy-token_9", b"v10")],
            host_prefix=False,
        )
        jar = CookieProvider(db, password=PASSWORD, host_suffixes=("taobao.com",)).load()
        self.assertEqual(jar.get("_m_h5_tk", "h5api.m.taobao.com"), "legacy-token_9")

    def test_v20_is_skipped_and_v10_still_works(self):
        db = Path(self.temp.name) / "mixed.db"
        make_cookie_db(
            db,
            [
                (".taobao.com", "_m_h5_tk", "good-token_1", b"v10"),
                (".taobao.com", "future", b"", b"v20not-supported"),
            ],
        )
        jar = CookieProvider(db, password=PASSWORD, host_suffixes=("taobao.com",)).load()
        self.assertEqual(jar.get("_m_h5_tk"), "good-token_1")
        self.assertEqual(jar.skipped_unsupported, 1)

    def test_wrong_password_fails_without_leaking_value(self):
        with self.assertRaises(CookieError) as caught:
            CookieProvider(
                self.db, password=b"wrong-password", host_suffixes=("taobao.com",)
            ).load()
        self.assertNotIn("secret-token_123", str(caught.exception))

    def test_password_loader_not_called_when_password_given(self):
        called = []
        provider = CookieProvider(
            self.db,
            password=PASSWORD,
            password_loader=lambda: called.append(True) or b"x",
            host_suffixes=("taobao.com",),
        )
        provider.load()
        self.assertEqual(called, [])

    def test_expired_cookie_is_not_returned(self):
        db = Path(self.temp.name) / "expired.db"
        make_cookie_db(
            db,
            [(".taobao.com", "_m_h5_tk", "expired_1", b"v10")],
        )
        import sqlite3

        connection = sqlite3.connect(db)
        connection.execute("UPDATE cookies SET expires_utc=1")
        connection.commit()
        connection.close()
        with self.assertRaises(CookieError):
            CookieProvider(db, password=PASSWORD, host_suffixes=("taobao.com",)).load()

    def test_temp_copy_is_removed(self):
        copy_dir = Path(self.temp.name) / "private-copy"
        copy_dir.mkdir()
        with mock.patch(
            "connectors.kit.cookies.tempfile.mkdtemp", return_value=str(copy_dir)
        ):
            jar = self.provider().load()
        self.assertGreater(jar.count(), 0)
        self.assertFalse(copy_dir.exists())

    def test_decrypt_v10_rejects_unknown_version(self):
        with self.assertRaises(CookieError):
            decrypt_v10_value(b"v11abc", ".taobao.com", derive_key(PASSWORD))


class CookieJarTests(unittest.TestCase):
    def test_domain_matching_and_specificity(self):
        jar = CookieJar(
            [
                Cookie(".taobao.com", "a", "parent"),
                Cookie("h5api.m.taobao.com", "a", "exact"),
                Cookie(".tmall.com", "b", "tmall"),
            ]
        )
        self.assertEqual(jar.get("a", "h5api.m.taobao.com"), "exact")
        self.assertIn("a=exact", jar.header_for("h5api.m.taobao.com"))
        self.assertNotIn("b=", jar.header_for("h5api.m.taobao.com"))

    def test_set_cookie_updates_in_memory(self):
        jar = CookieJar()
        jar.set_cookie("_m_h5_tk", "old_1", ".taobao.com")
        jar.set_cookie("_m_h5_tk", "new_2", ".taobao.com")
        self.assertEqual(jar.get("_m_h5_tk", "h5api.m.taobao.com"), "new_2")

    def test_set_cookie_preserves_distinct_domain_identity(self):
        jar = CookieJar([Cookie("h5api.m.taobao.com", "_m_h5_tk", "old_1")])
        jar.set_cookie("_m_h5_tk", "new_2", ".taobao.com")
        self.assertEqual(jar.get("_m_h5_tk", "h5api.m.taobao.com"), "old_1")
        self.assertEqual(jar.get("_m_h5_tk", "www.taobao.com"), "new_2")

    def test_has_any(self):
        jar = CookieJar([Cookie(".jd.com", "pt_key", "x")])
        self.assertTrue(jar.has_any(("pt_key", "pt_pin"), "order.jd.com"))
        self.assertFalse(jar.has_any(("pt_key",), "taobao.com"))


if __name__ == "__main__":
    unittest.main()
