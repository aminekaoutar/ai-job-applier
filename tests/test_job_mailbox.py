"""tools/job_mailbox.py and auto_apply's code typing: the site's code is found, typed, nothing else."""
import json
import sys
import tempfile
import time
import unittest
from email.message import EmailMessage
from email.utils import formatdate
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import auto_apply as aa  # noqa: E402
import job_mailbox as jm  # noqa: E402


def mail(frm: str, subject: str, body: str, when: float) -> bytes:
    m = EmailMessage()
    m["From"], m["Subject"], m["Date"] = frm, subject, formatdate(when)
    m.set_content(body)
    return bytes(m)


class FakeIMAP:
    def __init__(self, messages):
        self.messages = messages

    def __call__(self, host):
        return self

    def login(self, user, pwd):
        pass

    def select(self, box, readonly=True):
        return "OK", [b"1"]

    def search(self, charset, query):
        return "OK", [b" ".join(str(i + 1).encode() for i in range(len(self.messages)))]

    def fetch(self, mid, what):
        return "OK", [(b"1", self.messages[int(mid) - 1])]

    def logout(self):
        pass


class ExtractCode(unittest.TestCase):
    def test_french_and_english_codes(self):
        self.assertEqual(jm.extract_code("", "Voici votre code de vérification : 686448. Il expire dans 10 min."),
                         "686448")
        self.assertEqual(jm.extract_code("482913 is your verification code", ""), "482913")
        self.assertEqual(jm.extract_code("Bienvenue", "Votre code : 12 34"), None)  # no code standing alone
        self.assertIsNone(jm.extract_code("Newsletter", "Offres du 2026 à Paris 75008"))

    def test_sender_must_be_the_site(self):
        msg = jm.email.message_from_bytes(mail("HelloWork <noreply@mail.hellowork.com>", "s", "b", time.time()))
        self.assertTrue(jm._from_site(msg, "www.hellowork.com"))
        self.assertFalse(jm._from_site(msg, "www.welcometothejungle.com"))
        fake = jm.email.message_from_bytes(mail("x@hellowork.com.evil.io", "s", "b", time.time()))
        self.assertFalse(jm._from_site(fake, "hellowork.com"))


class LatestCode(unittest.TestCase):
    def test_newest_code_from_the_site_after_the_request(self):
        now = time.time()
        msgs = [mail("noreply@hellowork.com", "Votre code", "code de vérification : 111111", now - 3600),   # too old
                mail("noreply@welcometothejungle.com", "Your code", "verification code 222222", now),        # other site
                mail("noreply@hellowork.com", "Votre code", "code de vérification : 333333", now + 5)]
        with tempfile.TemporaryDirectory() as d:
            cfg = Path(d) / "mailbox.json"
            cfg.write_text(json.dumps({"address": "jobs@example.com", "imap_host": "imap.example.com"}))
            with mock.patch.dict(jm.os.environ, {"JOB_MAILBOX_PASSWORD": "app-pass"}):
                code = jm.latest_code("www.hellowork.com", now - 10, timeout_s=1, config_path=cfg,
                                      imap_factory=FakeIMAP(msgs))
        self.assertEqual(code, "333333")

    def test_unconfigured_mailbox_is_a_no_op(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(jm.latest_code("hellowork.com", time.time(), config_path=Path(d) / "none.json"))


class TypeCode(unittest.TestCase):
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

    def test_one_box_per_digit(self):
        page = self.browser.new_page()
        boxes = "".join(f'<input inputmode=numeric maxlength=1 id=d{i} '
                        f'oninput="if(this.value) document.getElementById(\'d{i + 1}\')?.focus()">' for i in range(6))
        page.set_content(f"<p>Entrez le code reçu</p>{boxes}")
        self.assertTrue(aa.type_code(page, "686448"))
        self.assertEqual("".join(page.input_value(f"#d{i}") for i in range(6)), "686448")
        page.close()

    def test_single_box(self):
        page = self.browser.new_page()
        page.set_content('<p>Enter the code we sent</p><input name=verificationCode autocomplete=one-time-code>')
        self.assertTrue(aa.type_code(page, "482913"))
        self.assertEqual(page.input_value("input"), "482913")
        page.close()


if __name__ == "__main__":
    unittest.main()
