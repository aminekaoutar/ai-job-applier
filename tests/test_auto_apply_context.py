"""tools/auto_apply.py: posting language, a CV in that language, the answer context,
LinkedIn skipped, CAPTCHA stops the run."""


import json
import sys
import tempfile
import unittest

from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import auto_apply as aa  # noqa: E402
import profile_store as ps  # noqa: E402

EN_POSTING = ("We are looking for a Data Engineer to join our team. You will build pipelines with Airflow "
              "and Spark, and you will work with our data scientists on the platform.")
FR_POSTING = ("Nous recherchons un Data Engineer pour notre équipe. Vous serez en charge des pipelines "
              "de données avec Airflow et Spark, dans une équipe pour la plateforme.")

FR_PROFILE = {
    "basics": {"name": "Test Candidate", "email": "t@example.com", "headline": "Ingénieur données",
               "summary": "Ingénieur données avec une expérience dans les pipelines pour la production.",
               "github": ""},
    "experience": [{"title": "Ingénieur données", "company": "Acme", "start": "2023-01", "end": "",
                    "bullets": ["Développé des pipelines de données avec Airflow pour la plateforme et les équipes"],
                    "skills": ["Airflow", "Python"]}],
    "skills": ["Python", "Airflow", "Spark"],
}


def make_profile_dir(d: str) -> Path:
    base = Path(d)
    (base / "tailored").mkdir()
    (base / "outreach").mkdir()
    ps.save_profile(FR_PROFILE, base)
    return base


class PostingLanguage(unittest.TestCase):
    def test_language_follows_the_posting(self):
        self.assertEqual(aa.posting_language(EN_POSTING), "English")
        self.assertEqual(aa.posting_language(FR_POSTING), "French")
        self.assertEqual(aa.posting_language(""), "French")  # French market default

    def test_cv_matches_language(self):
        full = {"language": "English", "polished": True, "labels_translated": True}
        self.assertTrue(aa.cv_matches_language(full, "English", "French"))
        # Headings in English but bullets still the French profile's: not an English CV.
        self.assertFalse(aa.cv_matches_language({"language": "English", "polished": False}, "English", "French"))
        # Bullets English but titles / degrees / "Présent" still French: not an English CV either.
        self.assertFalse(aa.cv_matches_language({"language": "English", "polished": True}, "English", "French"))
        self.assertTrue(aa.cv_matches_language({"language": "French", "polished": False}, "French", "French"))
        self.assertFalse(aa.cv_matches_language({"language": "French", "polished": True}, "English", "French"))


class LanguageCV(unittest.TestCase):
    def test_reuses_the_trackers_cv_when_already_in_the_language(self):
        with tempfile.TemporaryDirectory() as d:
            base = make_profile_dir(d)
            cv = ps.save_tailored({**ps.tailor(ps.load_profile(base), FR_POSTING, "Data Engineer", "Acme"),
                                   "id": "fr-cv"}, base)
            (base / "tailored" / "fr-cv.pdf").write_bytes(b"%PDF-1.4\n")
            with mock.patch.object(ps, "polish") as polish:
                pdf, got, note = aa.language_cv({"cv_id": "fr-cv"}, base, FR_POSTING, "French")
            polish.assert_not_called()
            self.assertEqual(pdf, (base / "tailored" / "fr-cv.pdf").resolve())
            self.assertEqual(got["id"], cv["id"])

    def test_english_posting_gets_a_new_english_cv(self):
        with tempfile.TemporaryDirectory() as d:
            base = make_profile_dir(d)
            # The tracker prepared an unpolished CV: English headings, French bullets.
            ps.save_tailored({**ps.tailor(ps.load_profile(base), EN_POSTING, "Data Engineer", "Acme"), "id": "mixed"}, base)
            (base / "tailored" / "mixed.pdf").write_bytes(b"%PDF-1.4\n")
            rec_path = base / "outreach" / "acme.json"
            rec_path.write_text(json.dumps({"cv_id": "mixed", "pdf": "tailored/mixed.pdf"}), encoding="utf-8")

            def fake_polish(cv, profile, max_bullets=4):
                self.assertEqual(cv["language"], "English")
                cv = json.loads(json.dumps(cv))
                cv["experience"][0]["bullets"] = ["Built Airflow data pipelines for the platform"]
                cv["polished"] = True
                return cv

            def fake_render(cv, base_dir):
                out = base_dir / "tailored" / f"{cv['id']}.pdf"
                out.write_bytes(b"%PDF-1.4\n")
                return out

            def fake_run(prompt, schema, timeout=300, task="claude"):
                self.assertEqual(task, "cv_translate")
                self.assertIn("Ingénieur données", prompt)
                return {"headline": "Data Engineer", "experience": [{"index": 0, "title": "Data Engineer", "end": ""}],
                        "education": [], "languages": []}

            import batch
            job = {"cv_id": "mixed", "_path": str(rec_path), "title": "Data Engineer", "company": "Acme", "key": "k1"}
            with mock.patch.object(ps, "polish", side_effect=fake_polish), \
                    mock.patch.object(aa.ps, "run_claude", side_effect=fake_run), \
                    mock.patch.object(batch, "render_pdf", side_effect=fake_render):
                pdf, cv, note = aa.language_cv(job, base, EN_POSTING, "English")
            self.assertEqual(cv["language"], "English")
            self.assertTrue(cv["polished"])
            self.assertTrue(cv["labels_translated"])
            self.assertEqual(cv["basics"]["headline"], "Data Engineer")
            self.assertEqual(cv["experience"][0]["title"], "Data Engineer")
            self.assertEqual(cv["experience"][0]["company"], "Acme")  # names are never translated
            self.assertEqual(cv["experience"][0]["bullets"], ["Built Airflow data pipelines for the platform"])
            self.assertTrue(pdf.is_file())
            self.assertNotEqual(cv["id"], "mixed")
            rec = json.loads(rec_path.read_text(encoding="utf-8"))
            self.assertEqual(rec["cv_id"], cv["id"])  # the Send list now points at the English CV
            self.assertIn("English", note)

    def test_no_cv_rather_than_a_wrong_language_cv(self):
        with tempfile.TemporaryDirectory() as d:
            base = make_profile_dir(d)
            with mock.patch.object(ps, "polish", side_effect=ps.ClaudeError("offline")):
                pdf, cv, note = aa.language_cv({"title": "Data Engineer", "company": "Acme"}, base, EN_POSTING, "English")
            self.assertIsNone(pdf)
            self.assertIn("English", note)


class AnswerContext(unittest.TestCase):
    def test_prompt_carries_language_posting_github_and_residence_facts(self):
        seen = {}

        def fake_run(prompt, schema, timeout=300, task="claude"):
            seen.update(prompt=prompt, task=task)
            return {"answers": [{"id": "f1", "value": "Oui"}], "unanswered": []}

        standing = {"nationality": "Moroccan", "residence_permit": "Carte de séjour pluriannuelle",
                    "work_authorization": "Authorised to work in France", "auto_submit": True}
        context = {"language": "English", "posting": EN_POSTING, "cv": {"summary": "Data engineer"},
                   "github": [{"name": "Airflow ETL", "skills": ["Airflow", "dbt"]}]}
        with mock.patch.object(aa.ps, "run_claude", side_effect=fake_run):
            answers, missing = aa.claude_answers([{"id": "f1", "kind": "radio", "label": "Work permit?",
                                                   "options": ["Oui", "Non"]}], FR_PROFILE, {"company": "Acme"},
                                                 standing, context)
        self.assertEqual(answers, {"f1": "Oui"})
        self.assertEqual(seen["task"], "form_answers")
        p = seen["prompt"]
        self.assertIn("in English - the language of the job posting", p)
        self.assertIn("Airflow ETL", p)
        self.assertIn("Moroccan", p)
        self.assertIn("Carte de séjour pluriannuelle", p)
        self.assertIn("pipelines with Airflow", p)
        self.assertNotIn("auto_submit", p.split("<standing_answers>")[1])  # only answer keys reach Claude

    def test_new_answer_keys_exist(self):
        for key in ("nationality", "residence_permit", "permit_valid_until", "work_authorization", "needs_sponsorship"):
            self.assertIn(key, aa.ANSWER_KEYS)


class Scope(unittest.TestCase):
    def test_linkedin_company_website_link_is_followed(self):
        # LinkedIn's redirect to the employer is decoded; plain external links pass through.
        self.assertEqual(aa.linkedin_external(
            "/jobs/view/externalApply/4471708493?url=https%3A%2F%2Fjobs.lever.co%2Facme%2F123&urlHash=x"),
            "https://jobs.lever.co/acme/123")
        self.assertEqual(aa.linkedin_external("https://careers.acme.com/apply/9"), "https://careers.acme.com/apply/9")

    def test_nothing_that_stays_on_linkedin_is_followed(self):
        self.assertIsNone(aa.linkedin_external("/jobs/view/4471708493/"))
        self.assertIsNone(aa.linkedin_external("/jobs/view/externalApply/1?url=https%3A%2F%2Fwww.linkedin.com%2Fx"))
        self.assertIsNone(aa.linkedin_external("javascript:void(0)"))

    def test_easy_apply_only_offer_is_never_entered(self):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            self.skipTest("playwright not installed")
        with sync_playwright() as pw:
            try:
                browser = pw.chromium.launch(headless=True)
            except Exception as exc:  # noqa: BLE001
                self.skipTest(f"Chromium unavailable: {exc}")
            page = browser.new_page()
            page.set_content("""<h1>ML Engineer</h1><button onclick="document.title='EASY'">Easy Apply</button>
                <a href="/jobs/view/123/">Similar jobs</a>""")
            self.assertFalse(aa.leave_linkedin(page, page.context))
            self.assertNotEqual(page.title(), "EASY")  # Easy Apply was not clicked
            browser.close()

    def test_linkedin_not_in_login_sites(self):
        self.assertFalse(any("linkedin" in u for u in aa.LOGIN_SITES))


class PageStates(unittest.TestCase):
    """Found by a dry run on real offers: Free-Work's expired redirect, WTTJ's CDN block, HelloWork's slow modal."""

    @classmethod
    def setUpClass(cls):
        try:
            from playwright.sync_api import sync_playwright
            cls.pw = sync_playwright().start()
            cls.browser = cls.pw.chromium.launch(headless=True)
        except Exception as exc:  # noqa: BLE001
            raise unittest.SkipTest(f"Playwright/Chromium unavailable: {exc}")

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def test_job_search_page_is_not_an_application_form(self):
        page = self.browser.new_page()
        page.set_content("""<form action="/fr/tech-it/jobs"><input name=query placeholder="Mots-clé, métier…">
            <select name=sort><option>date</option></select><select name=locale><option>fr</option></select>
            <button>Rechercher</button></form>""")
        self.assertFalse(aa.has_form(page))
        self.assertEqual(aa.collect(page), [])
        page.close()

    def test_expired_redirect_and_blocked_page(self):
        self.assertTrue(aa.EXPIRED_URL.search("https://www.free-work.com/fr/tech-it/jobs?expire=1&query=IA"))
        self.assertFalse(aa.EXPIRED_URL.search("https://www.free-work.com/fr/tech-it/job-mission/chef-de-projet-ia"))
        page = self.browser.new_page()
        page.set_content("<title>ERROR: The request could not be satisfied</title><h1>403 ERROR</h1>")
        self.assertEqual(aa.page_state(page), "blocked")
        page.close()

    def test_waits_for_a_form_that_renders_late(self):
        page = self.browser.new_page()
        page.set_content("""<div id=m></div><script>setTimeout(() => { document.getElementById('m').innerHTML =
            '<form><label>Prénom <input name=fn></label><label>Nom <input name=ln></label>' +
            '<label>Email <input type=email name=em></label></form>'; }, 1500)</script>""")
        self.assertTrue(aa.wait_for_fields(page, tries=4, pause_ms=800))
        page.set_content("<p>nothing to fill</p>")
        self.assertFalse(aa.wait_for_fields(page, tries=2, pause_ms=100))
        page.close()


class Captcha(unittest.TestCase):
    def frame(self, url, visible=True):
        f = mock.MagicMock()
        f.url = url
        f.frame_element.return_value.is_visible.return_value = visible
        return f

    def test_visible_challenge_is_detected(self):
        page = mock.MagicMock()
        page.frames = [self.frame("about:blank"),
                       self.frame("https://www.google.com/recaptcha/api2/anchor?k=x")]
        self.assertTrue(aa.has_captcha(page))

    def test_hidden_or_absent_challenge_is_ignored(self):
        page = mock.MagicMock()
        page.frames = [self.frame("https://www.google.com/recaptcha/api2/anchor?k=x", visible=False),
                       self.frame("https://jobs.example.com/apply")]
        self.assertFalse(aa.has_captcha(page))


if __name__ == "__main__":
    unittest.main()
