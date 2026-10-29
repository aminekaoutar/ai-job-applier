"""Tests for tools/outreach.py and the posting fetchers in tools/job_finder.py (offline)."""
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tools"))

import job_finder as jf  # noqa: E402
import outreach as out  # noqa: E402
import profile_store as ps  # noqa: E402

HOME = """<html><body><a href="/nous-contacter">Contact</a><a href="https://other.com/contact">x</a>
<a href="/blog/post">blog</a><img src="logo@2x.png"><a href="mailto:hello@acme.fr">hi</a></body></html>"""
CONTACT = """<p>Recrutement : recrutement [at] acme [dot] fr</p><p>noreply@acme.fr</p>
<p>Partenaire : someone@gmail.com</p><a href="mailto:rh@jobs.acme.fr?subject=x">RH</a>"""


class Emails(unittest.TestCase):
    def test_extracts_plain_obfuscated_and_mailto_and_drops_junk(self):
        found = out.emails_in(HOME + CONTACT)
        self.assertIn("hello@acme.fr", found)
        self.assertIn("recrutement@acme.fr", found)
        self.assertIn("rh@jobs.acme.fr", found)
        self.assertNotIn("noreply@acme.fr", found)
        self.assertFalse(any(e.endswith(".png") for e in found))

    def test_spelled_out_posting_address_needs_the_company_domain(self):
        text = "Envoyez vos candidatures à freelance à insyco . fr avec un argumentaire. Rendez-vous à paris . fr"
        self.assertEqual(out.spelled_emails(text, "INSYCO"), {"freelance@insyco.fr"})

    def test_recruiting_addresses_rank_first(self):
        emails = ["contact@acme.fr", "jean@acme.fr", "recrutement@acme.fr"]
        self.assertEqual(sorted(emails, key=out.rank)[0], "recrutement@acme.fr")

    def test_registrable_domain(self):
        self.assertEqual(out.registrable("jobs.acme.fr"), "acme.fr")
        self.assertEqual(out.registrable("www.acme.co.uk"), "acme.co.uk")


class Crawl(unittest.TestCase):
    def test_only_company_domain_and_robots_respected(self):
        pages = {"https://www.acme.fr/": HOME, "https://www.acme.fr/nous-contacter": CONTACT}

        def fake_fetch(url, timeout=15):
            return (url, pages[url]) if url in pages else ("", "")

        def fake_gate(url):
            return (1, "DISALLOWED for * - do not retry") if "recrutement" in url else (0, "ALLOWED")

        import robots_check
        with mock.patch.object(out, "_fetch", side_effect=fake_fetch), mock.patch.object(robots_check, "gate", side_effect=fake_gate):
            contacts, notes = out.crawl_contacts("https://www.acme.fr")
        emails = [c["email"] for c in contacts]
        self.assertEqual(emails[0], "recrutement@acme.fr")
        self.assertNotIn("someone@gmail.com", emails)  # third-party address on the page
        self.assertTrue(any("DISALLOWED" in n for n in notes))
        self.assertTrue(all(c["source"].startswith("https://www.acme.fr") for c in contacts))

    def test_name_lookup_needs_an_exact_match(self):
        items = [{"name": "Bee Zen", "domain": "beezendrinks.com"}, {"name": "Alten Group", "domain": "alten.com"}]
        resp = mock.MagicMock()
        resp.__enter__.return_value.read.return_value = __import__("json").dumps(items).encode()
        with mock.patch.object(out.urllib.request, "urlopen", return_value=resp):
            self.assertEqual(out.company_site_from_name("ALTEN")["url"], "https://alten.com")
            self.assertIsNone(out.company_site_from_name("Beezen"))


class Draft(unittest.TestCase):
    def test_template_fallback_uses_profile_facts_only(self):
        p = ps.empty_profile()
        p["basics"].update(name="Amine Kaoutar", headline="Développeur GenAI", phone="+33 6 00 00 00 00")
        p["skills"] = ["Python", "LangChain", "RAG"]
        p["projects"] = [{"name": "Application RAG", "bullets": [], "skills": []}]
        opp = {"title": "AI Engineer", "company": "Acme", "url": "https://x"}
        with mock.patch.object(ps, "run_claude", side_effect=ps.ClaudeError("offline")):
            d = out.draft_email(p, opp, "Nous recherchons un ingénieur IA pour notre équipe.", "rh@acme.fr", None)
        self.assertEqual(d["method"], "template")
        self.assertIn("Application RAG", d["body"])
        self.assertIn("AI Engineer", d["subject"])
        self.assertEqual(d["to"], "rh@acme.fr")


class Postings(unittest.TestCase):
    def test_linkedin_description_is_read_from_the_guest_page(self):
        page = ('<h2 class="top-card-layout__title">AI Engineer</h2><a class="topcard__org-name-link" href="x">Acme</a>'
                '<div class="show-more-less-html__markup"><p>Build <b>RAG</b> agents</p><div><ul><li>Python</li></ul></div></div>')
        with mock.patch.object(jf, "_get_text", return_value=page):
            d = jf.posting_details("https://fr.linkedin.com/jobs/view/ai-engineer-at-acme-4471381677")
        self.assertEqual(d["source"], "linkedin")
        self.assertIn("Build RAG agents", d["text"])
        self.assertIn("- Python", d["text"])

    def test_jsonld_jobposting_with_company_site(self):
        page = ('<script type="application/ld+json">{"@graph":[{"@type":"JobPosting","title":"Dev IA",'
                '"description":"<p>LangChain et Docker</p>","hiringOrganization":{"name":"Acme","sameAs":"https://acme.fr"}}]}</script>')
        import robots_check
        with mock.patch.object(jf, "_get_text", return_value=page), mock.patch.object(robots_check, "gate", return_value=(0, "ALLOWED")):
            d = jf.posting_details("https://www.welcometothejungle.com/fr/companies/acme/jobs/dev-ia_paris")
        self.assertIn("LangChain et Docker", d["text"])
        self.assertEqual(d["company_url"], "https://acme.fr")

    def test_robots_disallowed_page_is_not_fetched(self):
        import robots_check
        with mock.patch.object(robots_check, "gate", return_value=(1, "DISALLOWED")), mock.patch.object(jf, "_get_text") as get:
            d = jf.posting_details("https://example.com/job/1")
        get.assert_not_called()
        self.assertEqual(d["text"], "")


if __name__ == "__main__":
    unittest.main()
