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

    def test_spare_upload_box_after_cv_attached_does_not_block(self):
        # HelloWork: once the CV is in, a required "Utiliser un autre CV" file box appears, empty.
        page = self.browser.new_page()
        page.set_content("""<form><label>Prénom <input name=fn required value="Amine"></label>
            <p>cv.pdf ajouté</p><label>Utiliser un autre CV <input type=file name=upload required></label>
            <label><input type=checkbox name=cgu required checked> J'accepte</label></form>""")
        self.assertTrue(aa.has_required_empty(page))            # no CV attached by us: still blocks
        self.assertFalse(aa.has_required_empty(page, cv_attached=True))
        page.set_content("<form><input name=fn required></form>")
        self.assertTrue(aa.has_required_empty(page, cv_attached=True))  # other empty fields still block
        page.close()

    HELLOWORK_LIKE = """<header><button onclick="document.title='HEADER'">Postuler</button></header>
        <section><p>Vous avez ces compétences ?</p>
          <label><input type=checkbox name=skill1> Création d'une base documentaire</label>
          <label><input type=checkbox name=skill2> Python</label></section>
        <form onsubmit="document.title='SENT';return false">
          <label>Prénom <input name=fn required></label><label>Nom <input name=ln required></label>
          <label>Email <input type=email name=em required></label>
          <label><input type=checkbox name=cgu required> J'accepte de créer un compte</label>
          <button type=submit>Continuer ma candidature</button></form>"""

    def test_job_page_chips_are_not_form_fields(self):
        page = self.browser.new_page()
        page.set_content(self.HELLOWORK_LIKE)
        labels = [f["label"] for _, f in aa.collect(page)]
        self.assertIn("Prénom", labels)
        self.assertFalse(any("base documentaire" in l or l.strip() == "Python" for l in labels), labels)
        page.close()

    def test_only_the_forms_own_button_is_clicked(self):
        page = self.browser.new_page()
        page.set_content(self.HELLOWORK_LIKE)
        for sel, v in (("input[name=fn]", "Amine"), ("input[name=ln]", "Kaoutar"), ("input[name=em]", "a@b.co")):
            page.fill(sel, v)
        page.check("input[name=cgu]")
        outcome, _ = aa.submit_step(page)
        self.assertEqual(page.title(), "SENT")  # "Continuer ma candidature" inside the form, not the header
        self.assertIn(outcome, {"submitted_unconfirmed", "submitted"})
        page.close()

    def test_the_filled_forms_button_wins_over_other_forms(self):
        # HelloWork: a job-alert form and a sticky "Postuler" form come before the application form.
        page = self.browser.new_page()
        page.set_content("""<form id=alert onsubmit="document.title='ALERT';return false"><input name=q>
              <button type=submit>Postuler</button></form>
            <form id=step onsubmit="document.title='SENT';return false">
              <label>Prénom <input name=fn></label><label>Nom <input name=ln></label>
              <label>Email <input type=email name=em></label></form>
            <button type=submit form=step>Continuer ma candidature</button>""")
        for _, f in aa.collect(page):  # marks the application form's fields with data-aa-id
            pass
        for sel, v in (("input[name=fn]", "A"), ("input[name=ln]", "B"), ("input[name=em]", "a@b.co")):
            page.fill(sel, v)
        aa.submit_step(page)
        self.assertEqual(page.title(), "SENT")
        page.close()

    def test_logged_in_form_prefilled_names_and_this_jobs_cv_selected(self):
        page = self.browser.new_page()
        page.set_content("""<form id=s>
            <label>Prénom <input name=fn required value=Amine></label>
            <label>Nom <input name=ln required value=Kaoutar></label>
            <label>CV <select name=cvId required><option value=old selected>finaxys-dev-python.pdf</option></select></label>
            <label>Utiliser un nouveau CV <input type=file name=upload
              onchange="const n = this.files[0].name; setTimeout(() => { const old = document.querySelector('select[name=cvId]');
                const s = document.createElement('select'); s.name = 'cvId'; s.required = true;
                s.add(new Option('finaxys-dev-python.pdf', 'old')); s.add(new Option(n, 'new'));
                old.replaceWith(s); }, 600)"></label></form>""")
        with tempfile.TemporaryDirectory() as d:
            cv = Path(d) / "2026-11-01-groupe-sii-stage-ia.pdf"
            cv.write_bytes(b"%PDF-1.4\n")
            with mock.patch.object(aa, "claude_answers", lambda *a, **k: ({}, {})):
                rep = aa.fill_page(page, {"basics": {"name": "Amine Kaoutar"}}, {}, cv, set(), False)
        self.assertEqual([m for m in rep["missing"] if m[1]], [])          # prefilled names are not "missing"
        self.assertEqual(page.eval_on_selector("select[name=cvId]", "e => e.value"), "new")  # this job's CV
        self.assertTrue(rep["cv_attached"])
        page.close()

    def test_another_jobs_cv_never_stays_selected(self):
        page = self.browser.new_page()
        page.set_content("""<form><label>Prénom <input name=fn value=A></label><label>Nom <input name=ln value=B></label>
            <label>CV <select name=cvId required><option selected>finaxys-dev-python.pdf</option></select></label></form>""")
        with tempfile.TemporaryDirectory() as d:
            cv = Path(d) / "groupe-sii.pdf"
            cv.write_bytes(b"%PDF-1.4\n")
            with mock.patch.object(aa, "claude_answers", lambda *a, **k: ({}, {})):
                rep = aa.fill_page(page, {"basics": {}}, {}, cv, set(), False)
        self.assertTrue(any(m[1] and "groupe-sii.pdf" in m[2] for m in rep["missing"]), rep["missing"])
        page.close()

    def test_second_step_after_continue_is_filled_not_abandoned(self):
        # HelloWork (logged in): "Continuer ma candidature" swaps in a step asking only "Téléphone".
        page = self.browser.new_page()
        page.set_content("""<div id=frame><form id=s onsubmit="document.getElementById('frame').innerHTML =
              '<form id=s2 onsubmit=&quot;document.title=\\'SENT\\';return false&quot;>' +
              '<p>Groupe SII a besoin d\\'une information complémentaire</p>' +
              '<label>Téléphone <input name=phone required></label><button type=submit>Postuler</button></form>';
              return false">
            <label>Prénom <input name=fn value=A></label><label>Nom <input name=ln value=B></label>
            <label>Email <input type=email name=em value=a@b.co></label>
            <button type=submit>Continuer ma candidature</button></form></div>""")
        basics = {"name": "A B", "email": "a@b.co", "phone": "+33 7 00 00 00 00"}
        with mock.patch.object(aa, "claude_answers", lambda *a, **k: ({}, {})):
            done = set()
            aa.fill_page(page, {"basics": basics}, {}, None, done, False)
            outcome, _ = aa.submit_step(page)
            self.assertEqual(outcome, "next_page")
            rep = aa.fill_page(page, {"basics": basics}, {}, None, done, False)
        self.assertIn("Téléphone", rep["filled"])  # new step's field got a fresh id, not skipped
        self.assertEqual(page.input_value("input[name=phone]"), "+33 7 00 00 00 00")
        aa.submit_step(page)
        self.assertEqual(page.title(), "SENT")
        page.close()

    def test_hellowork_similar_offers_page_is_the_confirmation_never_submitted(self):
        page = self.browser.new_page()
        page.set_content("""<form id=s onsubmit="document.body.innerHTML = '<h2>Postulez directement à une ou plusieurs offres.</h2>' +
              '<p>Sélectionnez les offres qui vous intéressent, nous transmettons votre candidature aux recruteurs.</p>' +
              '<form onsubmit=&quot;document.title=\\'MULTI\\';return false&quot;><input type=checkbox checked> Sopra Steria' +
              '<input type=checkbox checked> Viveris<button type=submit>Postuler</button></form>'; return false">
            <label>Téléphone <input name=phone value=0759640034></label><button type=submit>Postuler</button></form>""")
        outcome, _ = aa.submit_step(page)
        self.assertEqual(outcome, "submitted")
        self.assertNotEqual(page.title(), "MULTI")  # the pre-ticked "apply to these too" form is never sent
        page.close()

    def test_phone_format_the_field_accepts(self):
        self.assertEqual(aa.phone_variants("+33 7 59 64 00 34"),
                         ["+33 7 59 64 00 34", "+33759640034", "0759640034", "07 59 64 00 34"])
        page = self.browser.new_page()
        # HelloWork-like: digits only, 10 max, error text under the field otherwise.
        page.set_content("""<form><label>Téléphone <input name=phone maxlength=10
            oninput="this.nextElementSibling.textContent = /[^0-9]/.test(this.value) ? 'Certains caractères ne sont pas acceptés.' : ''">
            <small></small></label></form>""")
        (frame, f), = aa.collect(page)
        self.assertTrue(aa.fill_one(frame, f, "+33 7 59 64 00 34", None))
        self.assertEqual(page.input_value("input[name=phone]"), "0759640034")
        page.close()

    def test_one_question_step_ignores_page_skill_chips(self):
        page = self.browser.new_page()
        page.set_content("""<section><label><input type=checkbox name=skill> Intelligence artificielle</label></section>
            <form><p>Groupe SII a besoin d'une information complémentaire</p>
              <label>Téléphone <input name=phone required></label><button>Postuler</button></form>""")
        self.assertEqual([f["label"] for _, f in aa.collect(page)], ["Téléphone"])
        page.close()

    def test_open_autocomplete_list_does_not_eat_the_submit_click(self):
        # HelloWork's email field opens a suggestion list; a click while it is open only closes it.
        page = self.browser.new_page()
        page.set_content("""<form id=s onsubmit="document.title='SENT';return false">
            <label>Prénom <input name=fn value=A></label><label>Nom <input name=ln value=B></label>
            <label>Email <input type=email name=em value=a@b.co
              onfocus="document.getElementById('lb').hidden=false"></label></form>
            <ul id=lb role=listbox hidden style="position:fixed;inset:0;background:#fff">
              <li>a@b.co</li></ul>
            <button type=submit form=s>Continuer ma candidature</button>
            <script>document.addEventListener('keydown', e => { if (e.key === 'Escape')
              document.getElementById('lb').hidden = true; });</script>""")
        page.focus("input[name=em]")
        aa.submit_step(page)
        self.assertEqual(page.title(), "SENT")
        page.close()

    def test_emailed_code_step_is_reported_not_claimed_as_sent(self):
        page = self.browser.new_page()
        page.set_content("""<form id=s onsubmit="document.body.innerHTML='<p>Entrez le code reçu</p><p>Pour continuer, saisissez le code envoyé à a@b.co.</p><input name=code>';return false">
            <label>Prénom <input name=fn value=A></label><label>Nom <input name=ln value=B></label>
            <label>Email <input type=email name=em value=a@b.co></label>
            <button type=submit>Continuer ma candidature</button></form>""")
        outcome, _ = aa.submit_step(page)
        self.assertEqual(outcome, "email_code")
        page.close()

    def test_waits_while_the_candidate_types_the_code(self):
        page = self.browser.new_page()
        page.set_content("""<p id=t>Saisissez le code envoyé à a@b.co</p>
            <script>setTimeout(() => { document.getElementById('t').innerText = 'Merci pour votre candidature !'; }, 2000)</script>""")
        outcome, _ = aa.wait_for_code(page, timeout_s=12)
        self.assertEqual(outcome, "submitted")
        page.set_content("<p>Saisissez le code envoyé à a@b.co</p>")
        self.assertEqual(aa.wait_for_code(page, timeout_s=3)[0], None)  # nobody typed it: timeout, not sent
        page.close()

    def test_submit_button_tied_to_the_form_by_attribute(self):
        # HelloWork: <button type=submit form="offer-detail-main-step-form"> sits outside the <form>.
        page = self.browser.new_page()
        page.set_content("""<header><button onclick="document.title='HEADER'">Postuler</button></header>
            <form id=step onsubmit="document.title='SENT';return false">
              <label>Prénom <input name=fn value=A></label><label>Nom <input name=ln value=B></label>
              <label>Email <input type=email name=em value=a@b.co></label></form>
            <div><button type=submit form=step>Continuer ma candidature</button></div>""")
        aa.submit_step(page)
        self.assertEqual(page.title(), "SENT")
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
