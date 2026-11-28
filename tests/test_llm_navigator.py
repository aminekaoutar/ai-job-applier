"""tools/llm_navigator.py with a scripted Claude: reaches forms, never invents done, guardrails hold."""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import auto_apply as aa  # noqa: E402
import llm_navigator as nav  # noqa: E402

PROFILE = {"basics": {"name": "Amine Kaoutar", "email": "jobs@example.com", "phone": "0700000000", "photo": "p.png"},
           "skills": ["Python"]}
JOB = {"title": "AI Engineer", "company": "Acme"}

CAREERS = """<div id=cookies><button onclick="this.parentElement.remove()">Tout accepter</button></div>
<h1>AI Engineer</h1><a href="https://www.linkedin.com/jobs/view/1">Voir sur LinkedIn</a>
<button id=go onclick="document.getElementById('f').hidden=false">Je dépose ma candidature</button>
<form id=f hidden onsubmit="document.body.innerHTML='<h2>Merci pour votre candidature !</h2>';return false">
  <label>Prénom <input name=fn></label><label>Nom <input name=ln></label><label>Email <input type=email name=em></label>
  <label>Mot de passe <input type=password name=pw></label><button type=submit>Envoyer</button></form>"""


def scripted(steps):
    """A fake run_claude that answers each step from `steps`, given the element whose text/label matches."""
    calls = []

    def fake(prompt, schema, timeout=0, task=""):
        calls.append(prompt)
        want = steps[len(calls) - 1]
        if want.get("match"):
            block = prompt.split("<elements>\n")[1].split("\n</elements>")[0]
            elements = [nav.json.loads(line) for line in block.splitlines() if line.strip()]
            hit = next(e for e in elements if want["match"] in (e.get("x", "") + " " + e.get("l", "")))
            want = {**want, "target": hit["id"]}
        return {k: v for k, v in want.items() if k != "match"}
    return fake, calls


class Navigate(unittest.TestCase):
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

    def run_nav(self, html, steps, **kw):
        page = self.browser.new_page()
        page.set_content(html)
        fake, calls = scripted(steps)
        with mock.patch.dict(nav.os.environ, {"NAV_LLM": "claude"}), \
                mock.patch.object(nav.ps, "run_claude", side_effect=fake):
            out = nav.navigate(page, "goal", JOB, PROFILE, {"consent": True}, None, "French", log=lambda *_: None,
                               is_done=lambda p: bool(aa.DONE_TEXT.search(aa.page_text(p))), **kw)
        return out, page, calls

    def test_reaches_a_form_behind_an_unusual_button(self):
        (status, page, _), _, calls = self.run_nav(CAREERS, [
            {"action": "click", "match": "Tout accepter", "reason": "cookies"},
            {"action": "click", "match": "Je dépose ma candidature", "reason": "open the form"},
        ], reached=lambda p: aa.has_form(p))
        self.assertEqual(status, "reached")
        self.assertEqual(len(calls), 2)
        self.assertNotIn("p.png", calls[0])  # the photo is not sent
        self.assertEqual(PROFILE["basics"]["photo"], "p.png")  # and the profile itself is untouched
        page.close()

    def test_done_is_refused_without_a_confirmation_then_give_up(self):
        (status, page, reason), _, calls = self.run_nav(CAREERS, [
            {"action": "done", "reason": "looks fine"},
            {"action": "give_up", "reason": "salary question not in the answers"},
        ])
        self.assertEqual(status, "give_up")
        self.assertIn("done was refused", calls[1])
        page.close()

    def test_fills_and_submits_with_a_stored_password_it_never_sees(self):
        with mock.patch.object(nav, "site_password", return_value="S3cret-Pass!"):
            (status, page, _), _, calls = self.run_nav(CAREERS.replace(" hidden", ""), [
                {"action": "fill", "match": "Prénom", "value": "Amine", "reason": "name"},
                {"action": "fill", "match": "Nom", "value": "Kaoutar", "reason": "name"},
                {"action": "fill", "match": "Email", "value": "jobs@example.com", "reason": "email"},
                {"action": "fill_password", "match": "Mot de passe", "reason": "account"},
                {"action": "click", "match": "Envoyer", "reason": "submit"},
            ])
        self.assertEqual(status, "submitted")
        self.assertFalse(any("S3cret-Pass!" in c for c in calls))
        page.close()

    def test_linkedin_links_are_refused(self):
        (status, page, _), _, calls = self.run_nav(CAREERS, [
            {"action": "click", "match": "Voir sur LinkedIn", "reason": "try"},
            {"action": "give_up", "reason": "stop"},
        ])
        self.assertIn("refused: LinkedIn link", calls[1])
        page.close()

    def test_captcha_stops_before_asking_claude(self):
        page = self.browser.new_page()
        page.set_content(CAREERS)
        with mock.patch.object(nav.ps, "run_claude") as claude:
            status, _, reason = nav.navigate(page, "g", JOB, PROFILE, {}, None, "French", log=lambda *_: None,
                                             is_blocked=lambda p: "the site asks for a human check (CAPTCHA)")
        claude.assert_not_called()
        self.assertEqual(status, "blocked")
        page.close()

    def test_snapshot_hides_password_values_and_hidden_fields(self):
        page = self.browser.new_page()
        page.set_content('<input type=password value=hunter2 aria-label=pw><input hidden name=x><button>Go</button>')
        items = nav.snapshot(page)
        self.assertFalse(any("hunter2" in str(i) for i in items))
        self.assertEqual(len([i for i in items if i["tag"] == "input"]), 1)
        page.close()


class Provider(unittest.TestCase):
    def test_groq_when_a_key_exists_unless_forced(self):
        import groq_llm
        with mock.patch.dict(nav.os.environ, {"NAV_LLM": ""}), mock.patch.object(groq_llm, "available", return_value=True):
            self.assertEqual(nav.provider(), "groq")
        with mock.patch.dict(nav.os.environ, {"NAV_LLM": ""}), mock.patch.object(groq_llm, "available", return_value=False):
            self.assertEqual(nav.provider(), "claude")
        with mock.patch.dict(nav.os.environ, {"NAV_LLM": "claude"}):
            self.assertEqual(nav.provider(), "claude")

    def test_groq_failure_falls_back_to_claude_for_that_step(self):
        import groq_llm
        with mock.patch.dict(nav.os.environ, {"NAV_LLM": "groq"}), \
                mock.patch.object(groq_llm, "chat_json", side_effect=groq_llm.GroqError("429")), \
                mock.patch.object(nav.ps, "run_claude", return_value={"action": "wait", "reason": "r"}) as claude:
            self.assertEqual(nav.ask_step("p", log=lambda *_: None)["action"], "wait")
        claude.assert_called_once()


class GroqCalls(unittest.TestCase):
    def reply(self, content):
        return {"model": "openai/gpt-oss-120b", "choices": [{"message": {"content": content}}],
                "usage": {"prompt_tokens": 1200, "completion_tokens": 30}}

    def test_json_parsed_and_sent_with_json_mode(self):
        import groq_llm
        with mock.patch.object(groq_llm, "api_key", return_value="gsk_test"), \
                mock.patch.object(groq_llm, "_post",
                                  return_value=self.reply('{"action": "click", "target": "0:e3", "reason": "go"}')) as post:
            out = groq_llm.chat_json("prompt", nav.STEP_SCHEMA)
        self.assertEqual(out["target"], "0:e3")
        body = post.call_args[0][1]
        self.assertEqual(body["response_format"], {"type": "json_object"})
        self.assertEqual(body["temperature"], 0)

    def test_bad_json_is_retried_then_errors(self):
        import groq_llm
        with mock.patch.object(groq_llm, "api_key", return_value="gsk_test"), \
                mock.patch.object(groq_llm, "_post", return_value=self.reply("not json")) as post:
            with self.assertRaises(groq_llm.GroqError):
                groq_llm.chat_json("prompt", nav.STEP_SCHEMA)
        self.assertEqual(post.call_count, 2)

    def test_no_key_is_an_error_not_a_crash(self):
        import groq_llm
        with mock.patch.object(groq_llm, "api_key", return_value=None):
            with self.assertRaises(groq_llm.GroqError):
                groq_llm._call("p", {}, 5, "m")


class SitePassword(unittest.TestCase):
    def test_site_key(self):
        self.assertEqual(nav.site_key("https://careers.acme.co.uk/apply"), "acme.co.uk")
        self.assertEqual(nav.site_key("https://jobs.lever.co/acme/1"), "lever.co")


if __name__ == "__main__":
    unittest.main()
