"""Tests for tools/github_import.py (offline: the GitHub API is mocked)."""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tools"))

import github_import as gi  # noqa: E402
import profile_store as ps  # noqa: E402

REPOS = [
    {"name": "Application-RAG", "fork": False, "language": "Python", "description": "RAG app with LangChain and FAISS",
     "topics": [], "html_url": "https://github.com/u/Application-RAG", "created_at": "2026-01-02", "pushed_at": "2026-01-28"},
    {"name": "tcl-agent", "fork": False, "language": None, "description": "", "topics": [],
     "html_url": "https://github.com/u/tcl-agent", "created_at": "2025-11-30", "pushed_at": "2025-11-30"},
    {"name": "tcl_agent", "fork": False, "language": "Python", "description": "Assistant RAG pour TCL", "topics": [],
     "html_url": "https://github.com/u/tcl_agent", "created_at": "2025-11-01", "pushed_at": "2025-12-01"},
    {"name": "forked-lib", "fork": True, "language": "Python", "description": "not mine", "topics": [],
     "html_url": "https://github.com/u/forked-lib", "created_at": "2025-01-01", "pushed_at": "2025-01-01"},
]
READMES = {
    "Application-RAG": "# App\n![badge](x.svg)\nUses **LangChain**, FAISS and Docker.\n```bash\npip install x\n```",
    "tcl_agent": "Chatbot RAG avec Streamlit et embeddings. Parle anglais et français.",
}


def fake_get(path, accept="application/vnd.github+json"):
    if path.startswith("/users/u/repos"):
        return REPOS
    name = path.split("/")[3]
    if path.endswith("/readme"):
        return READMES.get(name)
    return None


class Parsing(unittest.TestCase):
    def test_username_from_url_or_name(self):
        self.assertEqual(gi.username_from("http://github.com/aminekaoutar"), "aminekaoutar")
        self.assertEqual(gi.username_from("github.com/aminekaoutar/"), "aminekaoutar")
        self.assertEqual(gi.username_from("aminekaoutar"), "aminekaoutar")
        with self.assertRaises(ValueError):
            gi.username_from("../etc/passwd")

    def test_readme_text_drops_code_and_images(self):
        t = gi.readme_text(READMES["Application-RAG"])
        self.assertIn("LangChain", t)
        self.assertNotIn("pip install", t)
        self.assertNotIn("badge", t)

    def test_clean_skills_drops_noise_and_case_duplicates(self):
        skills = gi.clean_skills(["Python", "Batchfile", "Dockerfile"], {"python", "rag", "english", "api", "langchain"})
        self.assertEqual(skills, ["Python", "LangChain", "RAG"])


class Fetch(unittest.TestCase):
    def test_skips_forks_merges_duplicate_repos_and_caches(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(gi, "_get", side_effect=fake_get) as get:
            repos = gi.fetch_repos("u", cache_dir=Path(d))
            names = [r["repo"] for r in repos]
            self.assertNotIn("forked-lib", names)
            self.assertEqual(sorted(names), ["Application-RAG", "tcl_agent"])  # tcl-agent folded into the richer twin
            rag = next(r for r in repos if r["repo"] == "Application-RAG")
            self.assertIn("FAISS", rag["skills"])
            calls = get.call_count
            gi.fetch_repos("u", cache_dir=Path(d))
            self.assertEqual(get.call_count, calls)  # second load served from the cache


class Projects(unittest.TestCase):
    def repos(self):
        with mock.patch.object(gi, "_get", side_effect=fake_get):
            return gi.fetch_repos("u")

    def test_claude_failure_falls_back_to_descriptions(self):
        with mock.patch.object(ps, "run_claude", side_effect=ps.ClaudeError("offline")):
            projects, warnings = gi.to_projects(self.repos(), use_claude=True)
        self.assertTrue(warnings)
        rag = next(p for p in projects if "RAG" in p["name"])
        self.assertEqual(rag["bullets"][0], "RAG app with LangChain and FAISS.")
        self.assertEqual(rag["url"], "https://github.com/u/Application-RAG")

    def test_merge_replaces_same_repo_and_dedupes_skills(self):
        profile = ps.empty_profile()
        profile["skills"] = ["Hugging Face", "Tailwind"]
        projects, _ = gi.to_projects(self.repos(), use_claude=False)
        projects[0]["skills"] += ["HuggingFace", "Tailwind CSS", "Groq"]
        profile, counts = gi.merge_into_profile(profile, projects, "u")
        self.assertEqual(counts["added"], 2)
        self.assertIn("Groq", counts["new_skills"])
        self.assertNotIn("HuggingFace", counts["new_skills"])
        self.assertNotIn("Tailwind CSS", counts["new_skills"])
        self.assertEqual(profile["basics"]["github"], "github.com/u")
        projects, _ = gi.to_projects(self.repos(), use_claude=False)
        profile, counts = gi.merge_into_profile(profile, projects, "u")
        self.assertEqual((counts["added"], counts["updated"]), (0, 2))

    def test_cv_project_named_in_readme_is_linked_not_duplicated(self):
        profile = ps.empty_profile()
        profile["projects"] = [{"id": "x", "name": "Application RAG – chatbot", "role": "Projet", "start": "", "end": "",
                                "url": "", "bullets": ["My own wording"], "skills": ["Python"]}]
        projects, _ = gi.to_projects(self.repos(), use_claude=False)
        profile, counts = gi.merge_into_profile(profile, projects, "u")
        self.assertEqual(counts["linked"], 1)
        cv_project = profile["projects"][0]
        self.assertEqual(cv_project["bullets"], ["My own wording"])
        self.assertEqual(cv_project["url"], "https://github.com/u/Application-RAG")
        self.assertIn("FAISS", cv_project["skills"])


if __name__ == "__main__":
    unittest.main()
