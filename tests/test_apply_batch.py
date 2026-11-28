"""tools/apply_batch.py: which offers are tried, and the batch stops once N are sent."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import apply_batch as ab  # noqa: E402


def setup_profile(d: Path, jobs, log=(), contract="CDI or freelance only - not internships (stage) or alternance"):
    (d / "outreach").mkdir()
    (d / "auto_apply").mkdir()
    for j in jobs:
        (d / "outreach" / f"{j['key']}.json").write_text(json.dumps(j), encoding="utf-8")
    (d / "auto_apply" / "log.jsonl").write_text("\n".join(json.dumps(e) for e in log), encoding="utf-8")
    (d / "answers.json").write_text(json.dumps({"contract_wanted": contract}), encoding="utf-8")


def job(key, url, title="Développeur Python", match=50, **kw):
    return {"key": key, "url": url, "title": title, "company": key.upper(), "match": match, **kw}


class Candidates(unittest.TestCase):
    def test_filters_and_order(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            setup_profile(d, [
                job("a", "https://www.hellowork.com/fr-fr/emplois/1.html", match=60),
                job("b", "https://www.hellowork.com/fr-fr/emplois/2.html", match=90),
                job("tried", "https://www.hellowork.com/fr-fr/emplois/3.html", match=99),
                job("sent", "https://www.hellowork.com/fr-fr/emplois/4.html", match=99, sent={"on": "x"}),
                job("li", "https://fr.linkedin.com/jobs/view/5", match=99),
                job("apec", "https://www.apec.fr/offre/6", match=99),
                job("stage", "https://www.hellowork.com/fr-fr/emplois/7.html", title="Stage Data IA", match=99),
                job("free", "https://www.hellowork.com/fr-fr/emplois/8.html", title="Consultant IA - Portage salarial", match=70),
            ], log=[{"key": "tried", "status": "expired"}, {"key": "x", "status": "dry_run"}])
            keys = [c["key"] for c in ab.candidates(d)]
        self.assertEqual(keys, ["b", "free", "a"])  # best match first; freelance kept (wanted)


class BatchRun(unittest.TestCase):
    def run_batch(self, outcomes, target, n_jobs=10):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            setup_profile(d, [job(f"j{i}", f"https://www.hellowork.com/fr-fr/emplois/{i}.html", match=100 - i)
                              for i in range(n_jobs)])
            seen = []

            def runner(j):
                seen.append(j["key"])
                return {"status": outcomes[len(seen) - 1] if len(seen) <= len(outcomes) else "expired"}
            out = ab.Batch(target, d, runner=runner).run(log=lambda *_: None)
        return out, seen

    def test_stops_once_n_are_sent(self):
        out, seen = self.run_batch(["submitted", "expired", "blocked", "submitted_unconfirmed", "submitted"], 2)
        self.assertEqual(out["sent"], 2)
        self.assertEqual(seen, ["j0", "j1", "j2", "j3"])  # stopped right after the 2nd send
        self.assertFalse(out["running"])

    def test_gives_up_after_three_times_n_tries(self):
        out, seen = self.run_batch(["expired"] * 10, 2)
        self.assertEqual(out["sent"], 0)
        self.assertEqual(len(seen), 6)

    def test_stop_request(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            setup_profile(d, [job(f"j{i}", f"https://www.hellowork.com/fr-fr/emplois/{i}.html") for i in range(5)])
            batch = ab.Batch(5, d, runner=lambda j: (batch.stop(), {"status": "submitted"})[1])
            out = batch.run(log=lambda *_: None)
        self.assertEqual(out["tried"], 1)
        self.assertTrue(out["stopped"])


if __name__ == "__main__":
    unittest.main()
