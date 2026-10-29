"""Tests for the job drawer routes in tools/tracker_app.py (offline: network, Chrome, Claude mocked)."""
import http.client
import json
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tools"))
sys.path.insert(0, str(REPO / "tests"))

import batch  # noqa: E402
import profile_store as ps  # noqa: E402
import tracker_app as ta  # noqa: E402
from test_profile_store import sample_profile  # noqa: E402

KEY = "acme_ai-engineer"
SEEN = {"seen": {KEY: {"title": "AI Engineer", "company": "Acme", "url": "https://www.free-work.com/fr/tech-it/x/job-mission/a",
                       "status": "new", "portal": "free-work-search", "first_seen": "2026-10-29", "location": "Paris"}}}


class JobView(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        base = Path(self.dir.name)
        self.profile_dir = base / "profile"
        ps.save_profile(sample_profile(), self.profile_dir)
        (base / "seen.json").write_text(json.dumps(SEEN), encoding="utf-8")
        self.tracker = base / "t.csv"
        port_ref = {"port": 0}
        handler = ta.make_handler(self.tracker, base / "seen.json", "tok", port_ref, threading.Lock(), self.profile_dir)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = port_ref["port"] = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.patches = [
            mock.patch.object(batch.job_finder, "posting_details",
                              return_value={"text": "Agents IA, LangChain, RAG. Écrire à jobs à acme . fr", "company_url": None, "emails": []}),
            mock.patch.object(batch.outreach, "find_contacts", return_value={"contacts": [], "site": None, "site_note": None, "notes": []}),
            mock.patch.object(batch, "render_pdf", return_value=None),
            mock.patch.object(batch.outreach, "draft_email", side_effect=lambda *a, **k: {"subject": "Candidature", "body": "Bonjour", "to": a[3], "method": "template"}),
            mock.patch.object(ps, "claude_path", return_value="/bin/claude"),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.server.shutdown()
        self.server.server_close()
        self.dir.cleanup()

    def req(self, method, path, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request(method, path, json.dumps(body) if body is not None else None,
                     {"Host": f"127.0.0.1:{self.port}", "Content-Type": "application/json", "X-Tracker-Token": "tok"})
        r = conn.getresponse()
        data = r.read()
        conn.close()
        return r.status, json.loads(data)

    def test_unprepared_then_prepared_then_edited_then_sent(self):
        status, view = self.req("GET", f"/api/job?key={KEY}")
        self.assertEqual(status, 200)
        self.assertFalse(view["prepared"])

        status, view = self.req("POST", "/api/job/prepare", {"key": KEY})
        self.assertEqual(status, 200, view)
        self.assertTrue(view["prepared"])
        self.assertEqual(view["contacts"][0]["email"], "jobs@acme.fr")
        self.assertEqual(view["draft"]["to"], "jobs@acme.fr")
        self.assertIn("RAG", view["cv"]["matched"])

        self.req("POST", "/api/outreach/save-draft", {"key": KEY, "to": "rh@acme.fr", "subject": "S2", "body": "B2"})
        _, view = self.req("GET", f"/api/job?key={KEY}")
        self.assertEqual((view["draft"]["to"], view["draft"]["subject"], view["draft"]["body"]), ("rh@acme.fr", "S2", "B2"))

        status, _ = self.req("POST", "/api/outreach/sent", {"key": KEY, "to": "rh@acme.fr"})
        self.assertEqual(status, 200)
        status, view = self.req("GET", f"/api/job?key={KEY}")  # applied jobs stay viewable
        self.assertEqual(status, 200)
        self.assertEqual(view["sent"]["to"], "rh@acme.fr")

    def test_unknown_job_is_404(self):
        self.assertEqual(self.req("GET", "/api/job?key=nope")[0], 404)


if __name__ == "__main__":
    unittest.main()
