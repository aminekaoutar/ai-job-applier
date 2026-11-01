"""Tests for the recruiter / manager finder (offline: LinkedIn, Hunter and Clearbit are mocked)."""
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
import gmail_drafts  # noqa: E402
import hunter  # noqa: E402
import job_finder  # noqa: E402
import outreach  # noqa: E402
import profile_store as ps  # noqa: E402
import tracker_app as ta  # noqa: E402
from test_profile_store import sample_profile  # noqa: E402

TEAM_HTML = """<div class="message-the-recruiter"><h2>Meet the hiring team</h2>
<a href="https://fr.linkedin.com/in/nadineaitaba?trk=x"><img alt=""></a>
<div><a href="https://fr.linkedin.com/in/nadineaitaba"><h3 class="base-main-card__title">Nadine Ait Aba</h3></a>
<h4 class="body-text">Talent Acquisition Specialist chez Whize</h4><button>Send message</button></div></div>"""


class Pieces(unittest.TestCase):
    def test_hiring_team_is_read_from_the_job_page(self):
        team = job_finder.linkedin_hiring_team(TEAM_HTML)
        self.assertEqual(team, [{"name": "Nadine Ait Aba", "headline": "Talent Acquisition Specialist chez Whize",
                                 "profile": "https://fr.linkedin.com/in/nadineaitaba"}])
        self.assertEqual(job_finder.linkedin_hiring_team("<html>no team</html>"), [])

    def test_guesses_need_a_full_name_and_follow_the_known_pattern(self):
        self.assertEqual(outreach.guess_emails("Kindiaba K.", "hudata.fr"), [])
        common = [g["email"] for g in outreach.guess_emails("Nadine Ait Aba", "whize.fr")]
        self.assertEqual(common, ["nadine.aitaba@whize.fr", "naitaba@whize.fr", "nadine@whize.fr"])
        known = outreach.guess_emails("Julie Martin", "acme.fr", "{f}{last}")
        self.assertEqual([g["email"] for g in known], ["jmartin@acme.fr"])
        self.assertTrue(all(g["kind"] == "guess" and "unverified" in g["via"] for g in known))

    def test_gmail_never_addresses_a_draft_to_a_guess_on_its_own(self):
        rec = {"draft": {"body": "B"}, "contacts": [{"email": "nadine.aitaba@whize.fr", "kind": "guess"}]}
        self.assertIsNone(gmail_drafts.recipient(rec))
        rec["contacts"].append({"email": "contact@whize.fr", "kind": "general"})
        self.assertEqual(gmail_drafts.recipient(rec), "contact@whize.fr")
        rec["draft"]["to"] = "nadine.aitaba@whize.fr"  # the user chose it in the panel
        self.assertEqual(gmail_drafts.recipient(rec), "nadine.aitaba@whize.fr")


class Route(unittest.TestCase):
    KEY = "whize_ai-engineer"

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        base = Path(self.dir.name)
        self.profile_dir = base / "profile"
        ps.save_profile(sample_profile(), self.profile_dir)
        seen = {"seen": {self.KEY: {"title": "AI Engineer", "company": "WHIZE", "status": "new", "first_seen": "2026-11-01",
                                    "url": "https://fr.linkedin.com/jobs/view/ai-engineer-at-whize-4471381677", "portal": "linkedin-search"}}}
        (base / "seen.json").write_text(json.dumps(seen), encoding="utf-8")
        batch.Store(self.profile_dir).save(self.KEY, {"key": self.KEY, "company": "WHIZE", "title": "AI Engineer",
                                                       "cv_id": "x", "site": "https://www.whize.fr", "contacts": []})
        port_ref = {"port": 0}
        handler = ta.make_handler(base / "t.csv", base / "seen.json", "tok", port_ref, threading.Lock(), self.profile_dir)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = port_ref["port"] = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        mock.patch.stopall()
        self.server.shutdown()
        self.server.server_close()
        self.dir.cleanup()

    def req(self, path, body):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("POST", path, json.dumps(body), {"Host": f"127.0.0.1:{self.port}", "Content-Type": "application/json",
                                                     "X-Tracker-Token": "tok"})
        r = conn.getresponse()
        data = json.loads(r.read())
        conn.close()
        return r.status, data

    def test_finder_without_hunter_gives_team_and_marked_guesses(self):
        mock.patch.object(job_finder, "_get_text", return_value=TEAM_HTML).start()
        mock.patch.object(hunter, "api_key", return_value=None).start()
        status, view = self.req("/api/outreach/find-person", {"key": self.KEY})
        self.assertEqual(status, 200, view)
        self.assertEqual(view["hiring_team"][0]["name"], "Nadine Ait Aba")
        guesses = [c for c in view["contacts"] if c["kind"] == "guess"]
        self.assertEqual(guesses[0]["email"], "nadine.aitaba@whize.fr")
        self.assertEqual(guesses[0]["linkedin"], "https://fr.linkedin.com/in/nadineaitaba")
        self.assertTrue(any("Hunter not connected" in n for n in view["find_person"]["notes"]))

    def test_a_bad_hunter_key_is_refused(self):
        status, data = self.req("/api/settings/hunter", {"key": "nope"})
        self.assertEqual(status, 400)


if __name__ == "__main__":
    unittest.main()
