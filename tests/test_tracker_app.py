"""Tests for tools/tracker_app.py - the local application-tracker interface."""
import http.client
import json
import sys
import tempfile
import threading
import unittest
from datetime import date
from http.server import ThreadingHTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tools"))

import tracker_app as ta  # noqa: E402

TODAY = date(2026, 10, 28)
HEADER = ",".join(ta.HEADER)


def row(**kw):
    base = {h: "" for h in ta.HEADER}
    base.update(kw)
    return base


class TrackerFile(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "job_search_tracker.csv"

    def tearDown(self):
        self.dir.cleanup()

    def test_missing_tracker_is_empty(self):
        header, rows, version = ta.read_tracker(self.path)
        self.assertEqual(header, ta.HEADER)
        self.assertEqual(rows, [])
        self.assertEqual(version, "")

    def test_legacy_thirteen_column_rows_are_padded_not_dropped(self):
        legacy = ",".join(ta.HEADER[:-1])
        self.path.write_text(f"{legacy}\n2026-10-01,Acme,AI,AI Engineer,CDI,online,applied,,,,,,https://x.io\n", encoding="utf-8")
        header, rows, _ = ta.read_tracker(self.path)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["company"], "Acme")
        self.assertNotIn("deadline", header)

    def test_write_keeps_existing_header_and_never_quotes(self):
        legacy = ta.HEADER[:-1]
        ta.write_tracker(self.path, legacy, [row(company='Acme, "Inc"', notes="line one\nline two")])
        lines = self.path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(lines[0], ",".join(legacy))
        self.assertEqual(len(lines), 2)
        self.assertNotIn('"', lines[1])
        self.assertEqual(len(lines[1].split(",")), len(legacy))

    def test_clean_field(self):
        self.assertEqual(ta.clean_field('a, "b"\r\nc'), "a; b c")
        self.assertEqual(ta.clean_field(None), "")


class Derive(unittest.TestCase):
    def d(self, ghost=21, **kw):
        return ta.derive(row(**kw), TODAY, ghost)

    def test_replies_count_as_responded(self):
        for status in ("interview", "offer", "hired", "rejected", "offer_declined"):
            self.assertEqual(self.d(date="2026-10-01", status=status)["response"], "responded", status)

    def test_quiet_applied_row_turns_ghosted_at_threshold(self):
        self.assertEqual(self.d(date="2026-10-20", status="applied")["response"], "waiting")
        self.assertEqual(self.d(date="2026-10-07", status="applied")["response"], "ghosted")
        self.assertEqual(self.d(ghost=60, date="2026-10-07", status="applied")["response"], "waiting")

    def test_no_response_and_legacy_spelling_are_ghosted(self):
        self.assertEqual(self.d(date="2026-10-27", status="no_response")["response"], "ghosted")
        self.assertEqual(self.d(date="2026-10-27", status="No Response")["status"], "no_response")

    def test_drafted_is_never_quiet_or_due(self):
        out = self.d(date="2026-09-01", status="drafted")
        self.assertEqual(out["response"], "not_sent")
        self.assertFalse(out["followup_due"])

    def test_dated_notes_reset_the_quiet_clock(self):
        out = self.d(date="2026-09-01", status="applied", notes="followed up 2026-10-25")
        self.assertEqual(out["days_quiet"], 3)
        self.assertEqual(out["followups"], 1)
        self.assertEqual(out["response"], "waiting")

    def test_followup_due_after_ten_days_until_two_sent(self):
        self.assertTrue(self.d(date="2026-10-15", status="applied")["followup_due"])
        self.assertFalse(self.d(date="2026-10-22", status="applied")["followup_due"])
        two = "followed up 2026-10-01 | followed up 2026-10-10"
        self.assertFalse(self.d(date="2026-09-01", status="applied", notes=two)["followup_due"])

    def test_rejected_after_interview_still_reached_interview(self):
        out = self.d(date="2026-09-01", status="rejected", notes="interview 2026-09-20 | rejected 2026-10-01")
        self.assertTrue(out["reached_interview"])

    def test_portal_from_source_or_channel(self):
        self.assertEqual(self.d(source="https://www.linkedin.com/jobs/view/1")["portal"], "LinkedIn")
        self.assertEqual(self.d(source="https://www.welcometothejungle.com/fr/companies/x/jobs/y")["portal"], "Welcome to the Jungle")
        self.assertEqual(self.d(source="https://fr.indeed.com/viewjob?jk=1")["portal"], "Indeed")
        self.assertEqual(self.d(source="https://www.hellowork.com/fr-fr/emplois/1.html")["portal"], "HelloWork")
        self.assertEqual(self.d(channel="referral")["portal"], "referral")


class Edits(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "job_search_tracker.csv"
        self.path.write_text(f"{HEADER}\n2026-10-01,Acme,AI,AI Engineer,CDI,online,applied,,,,,,,\n", encoding="utf-8")

    def tearDown(self):
        self.dir.cleanup()

    def version(self):
        return ta.read_tracker(self.path)[2]

    def test_status_change_appends_dated_note(self):
        ta.apply_edit(self.path, self.version(), "status", {"index": 0, "company": "Acme", "role": "AI Engineer", "status": "interview"}, TODAY)
        r = ta.read_tracker(self.path)[1][0]
        self.assertEqual(r["status"], "interview")
        self.assertEqual(r["notes"], "interview 2026-10-28")

    def test_followup_marker_matches_outcome_format(self):
        ta.apply_edit(self.path, self.version(), "followup", {"index": 0, "company": "Acme", "role": "AI Engineer"}, TODAY)
        self.assertEqual(ta.read_tracker(self.path)[1][0]["notes"], "followed up 2026-10-28")

    def test_add_defaults_to_applied_today(self):
        ta.apply_edit(self.path, self.version(), "add", {"company": "Mistral AI", "role": "Applied AI Engineer", "channel": "LinkedIn"}, TODAY)
        r = ta.read_tracker(self.path)[1][1]
        self.assertEqual((r["date"], r["status"], r["channel"]), ("2026-10-28", "applied", "LinkedIn"))

    def test_stale_version_is_a_conflict(self):
        with self.assertRaises(ta.Conflict):
            ta.apply_edit(self.path, "stale", "followup", {"index": 0, "company": "Acme", "role": "AI Engineer"}, TODAY)

    def test_moved_row_is_a_conflict(self):
        with self.assertRaises(ta.Conflict):
            ta.apply_edit(self.path, self.version(), "followup", {"index": 0, "company": "Other", "role": "AI Engineer"}, TODAY)

    def test_unknown_status_and_action_are_rejected(self):
        ref = {"index": 0, "company": "Acme", "role": "AI Engineer"}
        with self.assertRaises(ValueError):
            ta.apply_edit(self.path, self.version(), "status", {**ref, "status": "ghosted"}, TODAY)
        with self.assertRaises(ValueError):
            ta.apply_edit(self.path, self.version(), "delete", ref, TODAY)


class Opportunities(unittest.TestCase):
    def test_tracked_skipped_and_expired_jobs_are_left_out(self):
        with tempfile.TemporaryDirectory() as d:
            seen = Path(d) / "seen_jobs.json"
            seen.write_text(json.dumps({
                "a": {"title": "AI Engineer", "company": "Acme", "url": "https://x.io/a", "status": "ranked", "rank_score": 70},
                "b": {"title": "LLM Engineer", "company": "Beta", "url": "https://x.io/b", "status": "ranked", "rank_score": 82, "portal": "linkedin-search"},
                "c": {"title": "Old", "company": "Gamma", "url": "https://x.io/c", "status": "skipped"},
                "d": {"title": "Gone", "company": "Delta", "url": "https://x.io/d", "status": "expired"},
                "e": {"title": "New one", "company": "Eps", "url": "javascript:alert(1)", "status": "new"},
            }), encoding="utf-8")
            out = ta.read_opportunities(seen, {(ta.norm("Acme"), ta.norm("AI Engineer"))})
        self.assertEqual([o["key"] for o in out], ["b", "e"])
        self.assertEqual(out[0]["portal"], "LinkedIn")
        self.assertEqual(out[1]["url"], "")


class Server(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.tracker = Path(self.dir.name) / "t.csv"
        self.token = "test-token"
        port_ref = {"port": 0}
        handler = ta.make_handler(self.tracker, Path(self.dir.name) / "none.json", self.token, port_ref, threading.Lock())
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = port_ref["port"] = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.dir.cleanup()

    def req(self, method, path, body=None, headers=None, host=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        h = {"Host": host or f"127.0.0.1:{self.port}", **(headers or {})}
        conn.request(method, path, body=json.dumps(body) if body is not None else None, headers=h)
        r = conn.getresponse()
        data = r.read()
        conn.close()
        return r.status, data

    def test_page_embeds_token_and_data_endpoint_works(self):
        status, page = self.req("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"test-token", page)
        status, data = self.req("GET", "/api/data?ghost_days=30")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(data)["ghost_days"], 30)

    def test_foreign_host_is_refused(self):
        self.assertEqual(self.req("GET", "/api/data", host="evil.example:80")[0], 403)

    def test_edit_needs_token(self):
        body = {"version": "", "action": "add", "payload": {"company": "Acme", "role": "AI Engineer"}}
        self.assertEqual(self.req("POST", "/api/edit", body)[0], 403)
        status, _ = self.req("POST", "/api/edit", body, {"X-Tracker-Token": self.token, "Content-Type": "application/json"})
        self.assertEqual(status, 200)
        self.assertEqual(ta.read_tracker(self.tracker)[1][0]["company"], "Acme")


if __name__ == "__main__":
    unittest.main()
