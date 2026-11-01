"""Tests for tools/hunter.py - the Hunter API is mocked; no credits are used."""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tools"))

import hunter  # noqa: E402

HR = {"data": {"organization": "Acme", "pattern": "{first}.{last}", "emails": [
    {"value": "julie.martin@acme.fr", "confidence": 94, "first_name": "Julie", "last_name": "Martin",
     "position": "Talent Acquisition Manager", "department": "hr", "seniority": "senior",
     "verification": {"status": "valid"}, "sources": [{"uri": "https://acme.fr/equipe"}]},
    {"value": "guess@acme.fr", "confidence": 40, "position": "Recruiter", "department": "hr", "sources": []},
]}}
MGR = {"data": {"organization": "Acme", "emails": [
    {"value": "paul.durand@acme.fr", "confidence": 88, "first_name": "Paul", "last_name": "Durand",
     "position": "Head of Data & IA", "department": "it", "seniority": "executive",
     "verification": {"status": "accept_all"}, "sources": [{"uri": "https://blog.acme.fr/a"}, {"uri": "https://x"}]},
    {"value": "old@acme.fr", "confidence": 90, "position": "Sales", "department": "sales", "seniority": "junior",
     "verification": {"status": "invalid"}, "sources": []},
]}}


def fake_get(path, params):
    if path == "account":
        return {"data": {"plan_name": "Free", "requests": {"searches": {"used": 3, "available": 25}}}}
    return HR if params.get("department") == "hr" else MGR


class Hunter(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.cache = Path(self.dir.name)
        self.get = mock.patch.object(hunter, "_get", side_effect=fake_get).start()

    def tearDown(self):
        mock.patch.stopall()
        self.dir.cleanup()

    def test_recruiters_first_low_confidence_and_invalid_dropped(self):
        contacts = hunter.to_contacts(hunter.domain_search("acme.fr", self.cache))
        self.assertEqual([c["email"] for c in contacts], ["julie.martin@acme.fr", "paul.durand@acme.fr"])
        self.assertEqual([c["kind"] for c in contacts], ["recruiter", "hiring-manager"])
        self.assertEqual(contacts[0]["name"], "Julie Martin")
        self.assertTrue(contacts[0]["verified"])
        self.assertIn("94% confidence", contacts[0]["via"])

    def test_results_are_cached_per_domain(self):
        hunter.domain_search("acme.fr", self.cache)
        calls = self.get.call_count
        hunter.domain_search("acme.fr", self.cache)
        self.assertEqual(self.get.call_count, calls)

    def test_posting_address_stays_first(self):
        existing = [{"email": "jobs@acme.fr", "kind": "posting"}, {"email": "contact@acme.fr", "kind": "general"}]
        merged = hunter.merge_contacts(existing, hunter.to_contacts(hunter.domain_search("acme.fr", self.cache)))
        self.assertEqual([c["email"] for c in merged],
                         ["jobs@acme.fr", "julie.martin@acme.fr", "paul.durand@acme.fr", "contact@acme.fr"])

    def test_untouched_draft_is_readdressed_with_a_first_name(self):
        rec = {"company": "Acme", "site": "https://www.acme.fr", "contacts": [{"email": "contact@acme.fr", "kind": "general"}],
               "draft": {"to": "contact@acme.fr", "subject": "S", "body": "Madame, Monsieur,\n\nTexte."}}
        rec = hunter.enrich_record(rec, self.cache)
        self.assertEqual(rec["draft"]["to"], "julie.martin@acme.fr")
        self.assertTrue(rec["draft"]["body"].startswith("Bonjour Julie,"))
        self.assertEqual(rec["hunter"]["domain"], "acme.fr")

    def test_edited_or_sent_draft_is_left_alone(self):
        for extra in ({"draft": {"to": "me@x.fr", "body": "Madame, Monsieur,", "edited": True}},
                      {"draft": {"to": "me@x.fr", "body": "Madame, Monsieur,"}, "sent": {"on": "2026-11-01"}}):
            rec = {"company": "Acme", "site": "acme.fr", "contacts": [], **extra}
            rec = hunter.enrich_record(rec, self.cache)
            self.assertEqual(rec["draft"]["to"], "me@x.fr")
            self.assertEqual(rec["draft"]["body"], "Madame, Monsieur,")

    def test_quota_and_domain_parsing(self):
        self.assertEqual(hunter.quota()["available"], 25)
        self.assertEqual(hunter.domain_of("https://www.Acme.fr/carrieres"), "acme.fr")
        self.assertEqual(hunter.domain_of("rh@jobs.acme.fr"), "jobs.acme.fr")
        self.assertIsNone(hunter.domain_of("not a site"))

    def test_no_key_is_a_clear_error(self):
        mock.patch.stopall()
        with mock.patch.object(hunter, "api_key", return_value=None), self.assertRaises(hunter.HunterError):
            hunter._get("account", {})


if __name__ == "__main__":
    unittest.main()
