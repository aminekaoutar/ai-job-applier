"""Tests for tools/batch.py (offline: network, Chrome and Claude are mocked)."""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tools"))
sys.path.insert(0, str(REPO / "tests"))

import batch  # noqa: E402
import profile_store as ps  # noqa: E402
from test_profile_store import sample_profile  # noqa: E402

OPPS = [
    {"key": "acme_ai-engineer", "title": "AI Engineer", "company": "Acme", "url": "https://www.free-work.com/fr/tech-it/x/job-mission/a",
     "portal": "Free-Work", "match": 80},
    {"key": "acme_ml-engineer", "title": "ML Engineer", "company": "Acme", "url": "https://fr.linkedin.com/jobs/view/ml-at-acme-4471381677",
     "portal": "LinkedIn", "match": 60},
    {"key": "beta_dev-ia", "title": "Dev IA", "company": "Beta", "url": "https://fr.indeed.com/viewjob?jk=1", "portal": "Indeed", "match": 40},
]
POSTINGS = {
    OPPS[0]["url"]: {"text": "Agents IA avec LangChain, RAG et Kubernetes. Candidatures à jobs à acme . fr", "company_url": "https://acme.fr", "emails": []},
    OPPS[1]["url"]: {"text": "Machine learning avec PyTorch et Docker.", "company_url": None, "emails": []},
    OPPS[2]["url"]: {"text": "", "company_url": None, "emails": []},
}


class PrepareAll(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.base = Path(self.dir.name)
        ps.save_profile(sample_profile(), self.base)
        self.crawls = []

        def contacts(opp, details):
            self.crawls.append(opp["company"])
            found = [{"email": "rh@acme.fr", "source": "https://acme.fr/contact", "kind": "recruiting"}] if opp["company"] == "Acme" else []
            return {"contacts": found, "site": "https://acme.fr" if found else None, "site_note": None, "notes": []}

        self.patches = [
            mock.patch.object(batch.job_finder, "posting_details", side_effect=lambda url: POSTINGS[url]),
            mock.patch.object(batch.outreach, "find_contacts", side_effect=contacts),
            mock.patch.object(batch, "render_pdf", side_effect=lambda cv, base: base / "tailored" / f"{cv['id']}.pdf"),
            mock.patch.object(batch.outreach, "draft_email", side_effect=lambda *a, **k: {"subject": "S", "body": "B", "to": a[3], "method": "template"}),
            mock.patch.object(batch.time, "sleep"),
            mock.patch.object(ps, "claude_path", return_value="/bin/claude"),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.dir.cleanup()

    def test_every_job_gets_a_cv_and_known_emails_get_a_draft(self):
        stats = batch.prepare_all(OPPS, self.base)
        self.assertEqual((stats["cvs"], stats["pdfs"], stats["with_email"], stats["drafts"], stats["from_title"]), (3, 3, 2, 2, 1))
        self.assertEqual(self.crawls, ["Acme", "Beta"])  # one crawl per company
        recs = batch.Store(self.base).all()
        first = recs["acme_ai-engineer"]["contacts"]
        self.assertEqual(first[0], {"email": "jobs@acme.fr", "source": OPPS[0]["url"], "kind": "posting"})  # posting address first
        self.assertTrue(recs["beta_dev-ia"]["from_title"])
        self.assertNotIn("draft", recs["beta_dev-ia"])

    def test_second_run_skips_prepared_jobs_and_keeps_polished_cvs(self):
        cv = ps.tailor(sample_profile(), "LangChain", "AI Engineer", "Acme")
        cv.update(polished=True, opportunity={"key": "acme_ai-engineer"})
        cv = ps.save_tailored(cv, self.base)
        batch.prepare_all(OPPS, self.base)
        self.assertEqual(batch.Store(self.base).load("acme_ai-engineer")["cv_id"], cv["id"])
        stats = batch.prepare_all(OPPS, self.base)
        self.assertEqual(stats["skipped"], 3)


if __name__ == "__main__":
    unittest.main()
