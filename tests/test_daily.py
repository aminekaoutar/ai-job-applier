"""Tests for tools/daily.py and tools/install_daily.py (offline; nothing is scheduled)."""
import fcntl
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
import daily  # noqa: E402
import install_daily  # noqa: E402
import profile_store as ps  # noqa: E402
from test_profile_store import sample_profile  # noqa: E402

SEEN = {"seen": {
    "a_ai": {"title": "AI Engineer", "company": "A", "url": "https://x/a", "status": "new", "portal": "free-work-search", "first_seen": "2026-10-31"},
    "b_ml": {"title": "ML Engineer", "company": "B", "url": "https://x/b", "status": "new", "portal": "linkedin-search", "first_seen": "2026-10-31"},
}}
FOUND = {"queries": ["IA générative"], "found": 20, "added": 2, "relevant": 2, "errors": []}


class DailyRun(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        root = Path(self.dir.name)
        self.base = root / "profile"
        ps.save_profile(sample_profile(), self.base)
        self.seen = root / "seen.json"
        self.seen.write_text(json.dumps(SEEN), encoding="utf-8")
        self.tracker = root / "t.csv"
        self.patches = [mock.patch.object(daily.job_finder, "profile_search", return_value=FOUND),
                        mock.patch.object(daily, "notify")]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.dir.cleanup()

    def test_prepares_only_unprepared_jobs_and_writes_a_report(self):
        batch.Store(self.base).save("a_ai", {"key": "a_ai", "cv_id": "x", "pdf": "tailored/x.pdf", "contacts": []})
        stats = {"cvs": 1, "pdfs": 1, "with_email": 1, "drafts": 1}
        with mock.patch.object(daily.batch, "prepare_all", return_value=stats) as prep:
            report = daily.run(self.base, self.seen, self.tracker)
        self.assertEqual([o["key"] for o in prep.call_args[0][0]], ["b_ml"])
        self.assertEqual((report["new_jobs"], report["prepared"], report["drafts"]), (2, 1, 1))
        self.assertEqual(daily.latest_report(self.base)["drafts"], 1)
        daily.notify.assert_called_once()

    def test_every_ticked_region_is_searched(self):
        (daily.daily_dir(self.base) / "regions.json").write_text(json.dumps({"regions": ["paris", "luxembourg", "switzerland", "mars"]}))
        with mock.patch.object(daily.batch, "prepare_all", return_value={}):
            report = daily.run(self.base, self.seen, self.tracker)
        regions = [c.args[3] for c in daily.job_finder.profile_search.call_args_list]
        self.assertEqual(regions, ["paris", "luxembourg", "switzerland"])  # unknown "mars" ignored
        self.assertEqual(report["new_jobs"], 3 * FOUND["added"])

    def test_dry_run_prepares_nothing(self):
        with mock.patch.object(daily.batch, "prepare_all") as prep:
            report = daily.run(self.base, self.seen, self.tracker, dry_run=True)
        prep.assert_not_called()
        self.assertEqual(report["prepared"], 0)

    def test_a_second_run_while_one_is_going_is_skipped(self):
        lock = open(daily.daily_dir(self.base) / ".lock", "w")
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            self.assertEqual(daily.run(self.base, self.seen, self.tracker), {"skipped": True})
        finally:
            lock.close()


class Plist(unittest.TestCase):
    def test_runs_daily_py_at_the_given_time_with_a_usable_path(self):
        pl = install_daily.build_plist(7, 45)
        self.assertEqual(pl["StartCalendarInterval"], {"Hour": 7, "Minute": 45})
        self.assertTrue(pl["ProgramArguments"][1].endswith("tools/daily.py"))
        self.assertIn("/opt/homebrew/bin", pl["EnvironmentVariables"]["PATH"])
        self.assertEqual(pl["WorkingDirectory"], str(REPO))


if __name__ == "__main__":
    unittest.main()
