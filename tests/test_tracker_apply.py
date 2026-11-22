"""Tracker app Apply flow: answers file, queue, and recording a submitted application."""

import http.client
import json
import sys
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import tracker_app as ta  # noqa: E402

KEY = "acme_ai-engineer"
URL = "https://www.hellowork.com/fr-fr/emplois/1.html"


class ApplyServer(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        root = Path(self.dir.name)
        self.tracker = root / "t.csv"
        self.profile = root / "profile"
        (self.profile / "outreach").mkdir(parents=True)
        seen = root / "seen.json"
        seen.write_text(json.dumps({"seen": {KEY: {"title": "AI Engineer", "company": "Acme", "url": URL,
                                                    "status": "new", "portal": "hellowork"}}}), encoding="utf-8")
        (self.profile / "outreach" / f"{KEY}.json").write_text(json.dumps(
            {"key": KEY, "company": "Acme", "title": "AI Engineer", "url": URL, "cv_id": "c1", "pdf": "tailored/c1.pdf"}),
            encoding="utf-8")
        self.token = "tok"
        port_ref = {"port": 0}
        handler = ta.make_handler(self.tracker, seen, self.token, port_ref, threading.Lock(), self.profile)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = port_ref["port"] = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.dir.cleanup()

    def req(self, method, path, body=None, token=True):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        h = {"Host": f"127.0.0.1:{self.port}", "Content-Type": "application/json"}
        if token:
            h["X-Tracker-Token"] = self.token
        conn.request(method, path, body=json.dumps(body) if body is not None else None, headers=h)
        r = conn.getresponse()
        data = json.loads(r.read() or b"{}")
        conn.close()
        return r.status, data

    def wait_done(self):
        for _ in range(100):
            status = self.req("GET", "/api/apply/status")[1]
            if not status["running"]:
                return status
            time.sleep(0.05)
        self.fail("apply queue never finished")

    def test_answers_round_trip_and_need_token(self):
        self.assertEqual(self.req("POST", "/api/apply/answers", {"answers": {}}, token=False)[0], 403)
        status, out = self.req("POST", "/api/apply/answers", {"answers": {
            "salary_expectation": "55k EUR", "consent": True, "auto_submit": True, "evil": "x"}})
        self.assertEqual(status, 200)
        saved = json.loads((self.profile / "answers.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["salary_expectation"], "55k EUR")
        self.assertTrue(saved["consent"])
        self.assertNotIn("evil", saved)
        got = self.req("GET", "/api/apply/answers")[1]
        self.assertTrue(got["exists"])
        self.assertIn("notice_period", got["keys"])

    def test_submitted_application_is_logged_in_tracker_and_send_list(self):
        calls = []

        def fake_run(url, submit, profile_dir):
            calls.append((url, submit))
            return {"status": "submitted", "form": url + "#postuler", "left": []}

        with mock.patch.object(ta, "run_auto_apply", fake_run), mock.patch.object(ta, "in_docker", lambda: False):
            status, out = self.req("POST", "/api/apply/start", {"keys": [KEY], "submit": True})
            self.assertEqual(status, 200, out)
            self.assertEqual(out["added"], 1)
            final = self.wait_done()
        self.assertEqual(calls, [(URL, True)])
        self.assertEqual(final["results"][KEY]["status"], "submitted")
        rows = ta.read_tracker(self.tracker)[1]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["company"], "Acme")
        self.assertEqual(rows[0]["status"], "applied")
        self.assertEqual(rows[0]["channel"], "auto-apply")
        send = self.req("GET", "/api/sendlist")[1]["rows"]
        self.assertEqual(send[0]["sent"]["to"], "application form")
        self.assertEqual(send[0]["auto_apply"]["status"], "submitted")

    def test_unfinished_form_is_not_logged_as_applied(self):
        fake = lambda url, submit, profile_dir: {"status": "needs_you", "left": ["Visa - not in profile"]}  # noqa: E731
        with mock.patch.object(ta, "run_auto_apply", fake), mock.patch.object(ta, "in_docker", lambda: False):
            self.req("POST", "/api/apply/start", {"keys": [KEY], "submit": True})
            self.wait_done()
        self.assertEqual(ta.read_tracker(self.tracker)[1], [])
        row = self.req("GET", "/api/sendlist")[1]["rows"][0]
        self.assertFalse(row.get("sent"))
        self.assertEqual(row["auto_apply"]["left"], ["Visa - not in profile"])

    def test_refused_inside_docker(self):
        with mock.patch.object(ta, "in_docker", lambda: True):
            status, out = self.req("POST", "/api/apply/start", {"keys": [KEY]})
        self.assertEqual(status, 400)
        self.assertIn("Docker", out["error"])


if __name__ == "__main__":
    unittest.main()
