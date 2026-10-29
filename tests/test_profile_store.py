"""Tests for tools/profile_store.py and the tracker app's profile / CV-builder routes."""
import base64
import http.client
import io
import json
import subprocess
import sys
import tempfile
import threading
import unittest
import zipfile
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tools"))

import profile_store as ps  # noqa: E402
import tracker_app as ta  # noqa: E402

# What pypdf typically returns for a French CV: bullet symbols are lost.
CV_TEXT = """Sara Benali
Ingénieure IA / LLM
Paris, France · sara.benali@example.com · +33 6 12 34 56 78
linkedin.com/in/sara-benali
Profil
Ingénieure IA spécialisée en IA générative et agents.
Expérience professionnelle
Ingénieure IA - Capgemini
Sept 2023 - Aujourd'hui
Conception d'un assistant RAG avec LangChain et Azure OpenAI
Déploiement sur Kubernetes avec CI/CD GitLab
Data Scientist (stage) | BNP Paribas
Fév 2023 - Août 2023
Modèles de scoring avec scikit-learn et XGBoost
Formation
Master Data Science - Université Paris-Saclay
2021 - 2023
Projets
Agent de veille emploi | Projet personnel
2024
Agent multi-outils avec Claude Code et MCP
Compétences
Langages : Python, SQL
IA : PyTorch, LangChain, RAG
Langues
Français (natif), Anglais (C1)
"""


def make_docx(paragraphs, photo=True) -> bytes:
    from PIL import Image
    ns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    body = "".join(f'<w:p><w:r><w:t xml:space="preserve">{p}</w:t></w:r></w:p>' for p in paragraphs)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", f'<?xml version="1.0"?><w:document xmlns:w="{ns}"><w:body>{body}</w:body></w:document>')
        if photo:
            img = io.BytesIO()
            Image.new("RGB", (200, 240), (90, 120, 160)).save(img, "PNG")
            z.writestr("word/media/image1.png", img.getvalue())
            tiny = io.BytesIO()
            Image.new("RGB", (20, 20)).save(tiny, "PNG")  # an icon, never the photo
            z.writestr("word/media/image2.png", tiny.getvalue())
    return buf.getvalue()


def sample_profile():
    return ps.normalize_profile(ps.parse_local(CV_TEXT))


class LocalParser(unittest.TestCase):
    def setUp(self):
        self.p = sample_profile()

    def test_basics(self):
        b = self.p["basics"]
        self.assertEqual((b["name"], b["headline"]), ("Sara Benali", "Ingénieure IA / LLM"))
        self.assertEqual(b["email"], "sara.benali@example.com")
        self.assertIn("linkedin.com/in/sara-benali", b["linkedin"])

    def test_entries_split_by_dates_without_bullet_symbols(self):
        exp = self.p["experience"]
        self.assertEqual([(e["title"], e["company"]) for e in exp],
                         [("Ingénieure IA", "Capgemini"), ("Data Scientist (stage)", "BNP Paribas")])
        self.assertEqual((exp[0]["start"], exp[0]["end"]), ("Sept 2023", "Aujourd'hui"))
        self.assertEqual(len(exp[0]["bullets"]), 2)
        self.assertIn("kubernetes", exp[0]["skills"])
        self.assertEqual(self.p["education"][0]["school"], "Université Paris-Saclay")
        self.assertEqual(self.p["projects"][0]["name"], "Agent de veille emploi")

    def test_skills_and_languages(self):
        self.assertIn("PyTorch", self.p["skills"])
        self.assertEqual(self.p["languages"][1], {"language": "Anglais", "level": "C1"})


class Import(unittest.TestCase):
    def test_docx_import_extracts_text_and_photo(self):
        data = make_docx(CV_TEXT.splitlines())
        with tempfile.TemporaryDirectory() as d:
            out = ps.import_cv("cv.docx", data, "local", Path(d))
            self.assertEqual(out["profile"]["photo"], "photo.png")
            self.assertTrue((Path(d) / "photo.png").is_file())
            self.assertEqual(len(out["profile"]["experience"]), 2)
            self.assertTrue(any(Path(d, "uploads").iterdir()))

    def test_rejects_other_files_and_empty_text(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(ValueError):
                ps.import_cv("cv.txt", b"hello", "local", Path(d))
            with self.assertRaises(ValueError):
                ps.import_cv("cv.docx", make_docx(["x"], photo=False), "local", Path(d))

    def test_claude_failure_falls_back_to_local_parser(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(ps, "run_claude", side_effect=ps.ClaudeError("offline")):
            out = ps.import_cv("cv.docx", make_docx(CV_TEXT.splitlines()), "claude", Path(d))
        self.assertEqual(out["profile"]["source"]["method"], "local")
        self.assertTrue(any("offline" in w for w in out["warnings"]))


class RunClaude(unittest.TestCase):
    def run_with(self, stdout, rc=0):
        done = subprocess.CompletedProcess([], rc, stdout=stdout, stderr="")
        with mock.patch.object(ps, "claude_path", return_value="/bin/claude"), mock.patch.object(ps.subprocess, "run", return_value=done) as run:
            return ps.run_claude("prompt", {"type": "object"}), run

    def test_event_list_output(self):
        events = [{"type": "system"}, {"type": "result", "is_error": False, "structured_output": {"a": 1}}]
        out, run = self.run_with(json.dumps(events))
        self.assertEqual(out, {"a": 1})
        cmd = run.call_args[0][0]
        self.assertIn("--tools", cmd)
        self.assertEqual(cmd[cmd.index("--tools") + 1], "")

    def test_single_object_output(self):
        self.assertEqual(self.run_with(json.dumps({"result": 'here {"a": 2}'}))[0], {"a": 2})

    def test_errors_raise(self):
        with self.assertRaises(ps.ClaudeError):
            self.run_with(json.dumps({"is_error": True, "result": "nope"}))
        with self.assertRaises(ps.ClaudeError):
            self.run_with("", rc=1)


class Matching(unittest.TestCase):
    def setUp(self):
        p = sample_profile()
        self.terms, self.targets = ps.profile_terms(p), ps.target_words(p)

    def test_one_shared_buzzword_cannot_max_the_score(self):
        m = ps.match_job(self.terms, self.targets, "AI Red Teamer – Pentest LLM")
        self.assertLess(m["score"], 60)

    def test_feminine_title_matches_engineer_roles(self):
        self.assertIn("engineer", self.targets)
        m = ps.match_job(self.terms, self.targets, "AI Engineer (LLM / RAG) F/H")
        self.assertEqual(m["score"], 100)

    def test_french_synonyms(self):
        self.assertIn("generative ai", ps.terms_in("Ingénieur IA Générative"))
        self.assertIn("cybersecurity", ps.terms_in("connaissances en cybersécurité"))
        self.assertNotIn("go", ps.terms_in("go to market"))


class Tailoring(unittest.TestCase):
    POSTING = ("Vous concevrez des agents IA avec LangGraph et du RAG sur Azure OpenAI. "
               "Kubernetes, Python et Terraform. Anglais courant.")

    def test_reports_gaps_and_never_adds_them(self):
        cv = ps.tailor(sample_profile(), self.POSTING, "AI Engineer", "Acme")
        self.assertIn("terraform", cv["missing"])
        self.assertIn("rag", cv["matched"])
        self.assertNotIn("Terraform", " ".join(cv["skills"]))
        self.assertEqual(cv["language"], "French")
        self.assertEqual(cv["basics"]["headline"], "Ingénieure IA / LLM")  # own headline, not the job title
        self.assertEqual(cv["terms_total"], len(cv["matched"]) + len(cv["missing"]))

    def test_most_relevant_bullet_first(self):
        cv = ps.tailor(sample_profile(), "Kubernetes CI/CD GitLab expert", "DevOps", "Acme")
        self.assertIn("Kubernetes", cv["experience"][0]["bullets"][0])

    def test_render_escapes_markup(self):
        p = sample_profile()
        p["basics"]["name"] = "<script>alert(1)</script>"
        html = ps.render_cv(ps.tailor(p, self.POSTING), None)
        self.assertNotIn("<script>alert", html)
        self.assertIn("&lt;script&gt;", html)

    def test_tailored_ids_cannot_escape_the_folder(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(ps.load_tailored("../profile", Path(d)))
            cv = ps.save_tailored(ps.tailor(sample_profile(), self.POSTING, "AI Engineer", "Acme"), Path(d))
            self.assertRegex(cv["id"], r"^[a-z0-9-]+$")
            self.assertEqual(ps.load_tailored(cv["id"], Path(d))["company"], "Acme")


class Routes(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        base = Path(self.dir.name)
        self.profile_dir = base / "profile"
        port_ref = {"port": 0}
        handler = ta.make_handler(base / "t.csv", base / "seen.json", "tok", port_ref, threading.Lock(), self.profile_dir)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = port_ref["port"] = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.dir.cleanup()

    def req(self, method, path, body=None, token="tok"):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        headers = {"Host": f"127.0.0.1:{self.port}", "Content-Type": "application/json", "X-Tracker-Token": token}
        conn.request(method, path, json.dumps(body) if body is not None else None, headers)
        r = conn.getresponse()
        data = r.read()
        conn.close()
        return r.status, data

    def test_import_tailor_and_view(self):
        docx = base64.b64encode(make_docx(CV_TEXT.splitlines())).decode()
        status, data = self.req("POST", "/api/profile/import", {"filename": "cv.docx", "data": docx, "method": "local"})
        self.assertEqual(status, 200, data)
        self.assertTrue(json.loads(data)["has_content"])
        self.assertEqual(self.req("GET", "/profile/photo.png")[0], 200)

        status, data = self.req("POST", "/api/cv/tailor", {"posting": Tailoring.POSTING, "job_title": "AI Engineer", "company": "Acme"})
        self.assertEqual(status, 200, data)
        cv_id = json.loads(data)["cv"]["id"]
        status, page = self.req("GET", f"/cv/{cv_id}")
        self.assertEqual(status, 200)
        self.assertIn(b"Sara Benali", page)

    def test_save_keeps_server_side_photo_and_needs_token(self):
        self.assertEqual(self.req("POST", "/api/profile/save", {"profile": {}}, token="bad")[0], 403)
        status, data = self.req("POST", "/api/profile/save", {"profile": {"photo": "../../x.png", "skills": ["Python"]}})
        self.assertEqual(status, 200)
        self.assertIsNone(json.loads(data)["profile"]["photo"])

    def test_tailor_without_profile_is_refused(self):
        status, data = self.req("POST", "/api/cv/tailor", {"posting": Tailoring.POSTING})
        self.assertEqual(status, 400)


if __name__ == "__main__":
    unittest.main()
