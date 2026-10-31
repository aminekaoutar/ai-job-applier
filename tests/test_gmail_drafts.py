"""Tests for tools/gmail_drafts.py - Gmail and the Keychain are mocked; nothing is contacted."""
import email
import email.policy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tools"))
sys.path.insert(0, str(REPO / "tests"))

import batch  # noqa: E402
import gmail_drafts as gd  # noqa: E402
import profile_store as ps  # noqa: E402
from test_profile_store import sample_profile  # noqa: E402


class FakeIMAP:
    def __init__(self):
        self.appended, self.deleted, self.commands = [], [], []

    def list(self):
        return "OK", [b'(\\HasNoChildren) "/" "INBOX"', b'(\\Drafts \\HasNoChildren) "/" "[Gmail]/Brouillons"']

    def append(self, folder, flags, date, data):
        self.appended.append((folder, flags, email.message_from_bytes(data, policy=email.policy.default)))
        return "OK", [b""]

    def select(self, folder):
        self.commands.append(("SELECT", folder))
        return "OK", [b"1"]

    def uid(self, command, *args):
        self.commands.append((command, *args))
        if command == "SEARCH":
            return "OK", [b"42"]
        if command == "STORE":
            self.deleted.append(args[0])
        return "OK", [b""]

    def logout(self):
        pass


class GmailDrafts(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.base = Path(self.dir.name)
        ps.save_profile(sample_profile(), self.base)
        (self.base / "tailored").mkdir()
        (self.base / "tailored" / "cv-a.pdf").write_bytes(b"%PDF-1.4 fake")
        store = batch.Store(self.base)
        draft = {"to": "rh@acme.fr", "subject": "Candidature – AI Engineer", "body": "Bonjour,\nMon CV est joint."}
        store.save("a", {"key": "a", "company": "Acme", "title": "AI Engineer", "match": 90, "pdf": "tailored/cv-a.pdf",
                         "contacts": [{"email": "rh@acme.fr"}], "draft": draft})
        store.save("b", {"key": "b", "company": "Beta", "title": "Dev", "match": 50, "contacts": [{"email": "x@beta.fr"}],
                         "draft": {"to": "x@beta.fr", "subject": "S", "body": "B"}, "sent": {"on": "2026-10-30"}})
        store.save("c", {"key": "c", "company": "Gamma", "title": "Dev", "match": 70, "contacts": [], "draft": None})
        (self.base / "daily").mkdir()
        (self.base / "daily" / "gmail.json").write_text(json.dumps({"address": "me@gmail.com"}))
        self.imap = FakeIMAP()
        self.patches = [mock.patch.object(gd, "keychain_get", return_value="apppassword"),
                        mock.patch.object(gd, "connect", return_value=self.imap)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.dir.cleanup()

    def test_pushes_only_ready_unsent_jobs_into_the_flagged_drafts_folder_with_the_cv(self):
        result = gd.push(self.base)
        self.assertEqual(result["pushed"], 1)
        folder, flags, msg = self.imap.appended[0]
        self.assertEqual(folder, '"[Gmail]/Brouillons"')
        self.assertIn("\\Draft", flags)
        self.assertEqual((msg["To"], msg["Subject"]), ("rh@acme.fr", "Candidature – AI Engineer"))
        attachments = [p.get_filename() for p in msg.walk() if p.get_content_type() == "application/pdf"]
        self.assertEqual(len(attachments), 1)
        self.assertTrue(attachments[0].endswith(".pdf"))
        self.assertEqual({c[0] for c in self.imap.commands}, set())  # no SELECT/STORE/EXPUNGE on a first push
        self.assertTrue(batch.Store(self.base).load("a")["gmail_draft"]["message_id"])

    def test_never_pushes_the_same_job_twice(self):
        gd.push(self.base)
        self.assertEqual(gd.push(self.base)["pushed"], 0)
        self.assertEqual(len(self.imap.appended), 1)

    def test_explicit_update_replaces_only_its_own_earlier_draft(self):
        gd.push(self.base)
        old_id = batch.Store(self.base).load("a")["gmail_draft"]["message_id"]
        gd.push(self.base, 1, keys=["a"])
        search = next(c for c in self.imap.commands if c[0] == "SEARCH")
        self.assertIn(old_id, search[-1])
        self.assertEqual(self.imap.deleted, [b"42"])
        self.assertIn(("EXPUNGE", b"42"), self.imap.commands)
        self.assertEqual(len(self.imap.appended), 2)

    def test_not_set_up_is_an_error_not_a_crash(self):
        (self.base / "daily" / "gmail.json").unlink()
        with self.assertRaises(gd.GmailError):
            gd.push(self.base)


if __name__ == "__main__":
    unittest.main()
