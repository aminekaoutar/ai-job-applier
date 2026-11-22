"""tools/auto_apply.py: field answers, CV lookup, and a browser fill that never submits."""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import auto_apply as aa  # noqa: E402

BASICS = {"name": "Test Candidate", "email": "t@example.com", "phone": "+33 6 00 00 00 00",
          "linkedin": "https://linkedin.com/in/test", "location": "Paris", "github": "", "website": ""}

FORM = """<html><head><meta charset="utf-8"></head><body>
<form onsubmit="document.title='SUBMITTED';return false">
<label for=fn>Prénom *</label><input id=fn name=firstname required>
<label for=ln>Nom *</label><input id=ln name=lastname required>
<label>Email <input type=email name=email required></label>
<label>CV / Resume <input type=file name=cv></label>
<label>Lettre de motivation <input type=file name=cl></label>
<label for=lang>Niveau d'anglais</label>
<select id=lang><option></option><option>Courant</option><option>Bilingue</option></select>
<fieldset><legend>Avez-vous besoin d'un visa ? *</legend>
<label><input type=radio name=visa value=y required>Oui</label><label><input type=radio name=visa value=n>Non</label></fieldset>
<label><input type=checkbox name=gdpr required> J'accepte la politique de confidentialité</label>
<button type=submit>Envoyer ma candidature</button></form></body></html>"""


class LocalAnswers(unittest.TestCase):
    def ans(self, label, kind="text", name=""):
        return aa.local_answer({"label": label, "kind": kind, "name": name}, BASICS)

    def test_identity_fields(self):
        self.assertEqual(self.ans("Prénom *"), "Test")
        self.assertEqual(self.ans("Nom *"), "Candidate")
        self.assertEqual(self.ans("Last name"), "Candidate")
        self.assertEqual(self.ans("Full name"), "Test Candidate")
        self.assertEqual(self.ans("anything", kind="email"), "t@example.com")
        self.assertEqual(self.ans("Téléphone"), "+33 6 00 00 00 00")
        self.assertEqual(self.ans("LinkedIn profile URL"), "https://linkedin.com/in/test")

    def test_unknown_or_empty_goes_to_claude(self):
        self.assertIsNone(self.ans("Prétentions salariales"))
        self.assertIsNone(self.ans("GitHub"))  # empty in profile: never filled with ""
        self.assertIsNone(self.ans("Prénom", kind="select"))


class JobLookup(unittest.TestCase):
    def test_country_subdomains_and_linkedin_ids_match(self):
        with tempfile.TemporaryDirectory() as d:
            prof = Path(d)
            (prof / "outreach").mkdir()
            (prof / "tailored").mkdir()
            (prof / "tailored" / "x.pdf").write_bytes(b"%PDF-1.4\n")
            (prof / "outreach" / "x.json").write_text(json.dumps({
                "url": "https://fr.linkedin.com/jobs/view/ai-engineer-at-acme-4471708493",
                "company": "Acme", "pdf": "tailored/x.pdf"}), encoding="utf-8")
            job = aa.find_job("https://www.linkedin.com/jobs/view/4471708493/", prof)
            self.assertEqual(job.get("company"), "Acme")
            self.assertEqual(aa.cv_for(job, prof, None), (prof / "tailored" / "x.pdf").resolve())
            self.assertEqual(aa.find_job("https://example.com/other", prof), {})


class BrowserFill(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            from playwright.sync_api import sync_playwright
            cls.pw = sync_playwright().start()
            cls.browser = cls.pw.chromium.launch(headless=True)
        except Exception as exc:  # noqa: BLE001 - optional dependency
            raise unittest.SkipTest(f"Playwright/Chromium unavailable: {exc}")

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def test_fills_from_profile_attaches_cv_and_never_submits(self):
        page = self.browser.new_page()
        page.set_content(FORM)
        seen = {}

        def fake_claude(fields, profile, job):
            seen.update({f["label"]: f for f in fields})
            by = {f["label"]: f["id"] for f in fields}
            return ({by["Niveau d'anglais"]: "Courant"},
                    {by["Avez-vous besoin d'un visa ? *"]: "not in profile",
                     by["J'accepte la politique de confidentialité"]: "consent"})

        with tempfile.TemporaryDirectory() as d, mock.patch.object(aa, "claude_answers", fake_claude):
            cv = Path(d) / "cv.pdf"
            cv.write_bytes(b"%PDF-1.4\n")
            rep = aa.fill_page(page, {"basics": BASICS}, {}, cv, set(), dry=False)

        self.assertEqual(page.input_value("#fn"), "Test")
        self.assertEqual(page.input_value("#ln"), "Candidate")
        self.assertEqual(page.input_value("input[name=email]"), "t@example.com")
        self.assertEqual(page.eval_on_selector("#lang", "e => e.value"), "Courant")
        self.assertEqual(page.eval_on_selector("input[name=cv]", "e => e.files[0].name"), "cv.pdf")
        self.assertEqual(page.eval_on_selector("input[name=cl]", "e => e.files.length"), 0)
        self.assertFalse(page.is_checked("input[name=gdpr]"))
        self.assertFalse(page.is_checked("input[name=visa][value=y]"))
        # Each group carries its own question, not a neighbour's.
        self.assertIn("J'accepte la politique de confidentialité", seen)
        self.assertEqual(len(seen["Avez-vous besoin d'un visa ? *"]["options"]), 2)
        missing = {label for label, _, _ in rep["missing"]}
        self.assertIn("Avez-vous besoin d'un visa ? *", missing)
        self.assertNotEqual(page.title(), "SUBMITTED")
        page.close()


if __name__ == "__main__":
    unittest.main()
