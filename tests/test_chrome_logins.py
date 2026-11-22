"""tools/chrome_logins.py: only job-site cookie rows are copied, unchanged; never LinkedIn or others."""
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import chrome_logins as cl  # noqa: E402

SCHEMA = """CREATE TABLE meta(key LONGVARCHAR NOT NULL UNIQUE PRIMARY KEY, value LONGVARCHAR);
CREATE TABLE cookies(creation_utc INTEGER NOT NULL, host_key TEXT NOT NULL, top_frame_site_key TEXT NOT NULL,
  name TEXT NOT NULL, value TEXT NOT NULL, encrypted_value BLOB NOT NULL, path TEXT NOT NULL,
  expires_utc INTEGER NOT NULL, is_secure INTEGER NOT NULL, is_httponly INTEGER NOT NULL,
  UNIQUE (host_key, top_frame_site_key, name, path));
INSERT INTO meta VALUES ('version', '24');"""


def make_db(path: Path, rows):
    con = sqlite3.connect(path)
    con.executescript(SCHEMA)
    con.executemany("INSERT INTO cookies VALUES (1, ?, '', ?, '', ?, '/', 9, 1, 1)", rows)
    con.commit()
    con.close()


class CopyCookies(unittest.TestCase):
    def test_only_job_sites_copied_as_stored(self):
        with tempfile.TemporaryDirectory() as d:
            src, dest = Path(d) / "src", Path(d) / "dest"
            make_db(src, [(".hellowork.com", "sess", b"v10ENC1"), ("www.free-work.com", "auth", b"v10ENC2"),
                          (".linkedin.com", "li_at", b"v10SECRET"), (".mybank.fr", "bank", b"v10BANK"),
                          (".nothellowork.com", "x", b"v10X")])
            make_db(dest, [(".hellowork.com", "old", b"v10OLD"), (".google.com", "keep", b"v10G")])
            out = cl.copy_cookies(src, dest)
            con = sqlite3.connect(dest)
            rows = {(h, n): v for h, n, v in con.execute("SELECT host_key, name, encrypted_value FROM cookies")}
            con.close()
        self.assertEqual(out["hellowork.com"], 1)
        self.assertEqual(out["free-work.com"], 1)
        self.assertEqual(rows[(".hellowork.com", "sess")], b"v10ENC1")       # copied byte for byte
        self.assertNotIn((".hellowork.com", "old"), rows)                    # job-site rows replaced
        self.assertIn((".google.com", "keep"), rows)                         # other dest rows untouched
        self.assertNotIn((".linkedin.com", "li_at"), rows)                   # never LinkedIn
        self.assertNotIn((".mybank.fr", "bank"), rows)                       # never other sites
        self.assertNotIn((".nothellowork.com", "x"), rows)                   # no look-alike domains

    def test_linkedin_refused_and_version_mismatch_stops(self):
        with tempfile.TemporaryDirectory() as d:
            src, dest = Path(d) / "src", Path(d) / "dest"
            make_db(src, [])
            make_db(dest, [])
            with self.assertRaises(ValueError):
                cl.copy_cookies(src, dest, sites=("linkedin.com",))
            con = sqlite3.connect(dest)
            con.execute("UPDATE meta SET value='23'")
            con.commit()
            con.close()
            with self.assertRaises(RuntimeError):
                cl.copy_cookies(src, dest)


if __name__ == "__main__":
    unittest.main()
