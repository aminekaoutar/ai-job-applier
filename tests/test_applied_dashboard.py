"""tracker_app.applied_rows: the Auto-applied tab never claims an application went out without proof."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import tracker_app as ta  # noqa: E402


def write_log(base: Path, entries):
    d = base / "auto_apply"
    d.mkdir(parents=True)
    (d / "log.jsonl").write_text("\n".join(json.dumps(e) for e in entries) + "\nnot json\n", encoding="utf-8")


class AppliedRows(unittest.TestCase):
    def rows(self, entries):
        with tempfile.TemporaryDirectory() as d:
            write_log(Path(d), entries)
            return {r["key"]: r for r in ta.applied_rows(Path(d))}

    def test_latest_outcome_per_job_and_dry_runs_ignored(self):
        rows = self.rows([
            {"date": "2026-11-22", "key": "a", "company": "A", "status": "dry_run"},
            {"date": "2026-11-22", "key": "a", "company": "A", "status": "needs_you"},
            {"date": "2026-11-26", "key": "a", "company": "A", "status": "expired", "url": "https://x.io/a"},
            {"date": "2026-11-26", "key": "b", "company": "B", "status": "dry_run"},
        ])
        self.assertEqual(set(rows), {"a"})
        self.assertEqual(rows["a"]["group"], "closed")
        self.assertEqual(rows["a"]["attempts"], 2)
        self.assertEqual(rows["a"]["first"], "2026-11-22")

    def test_unconfirmed_is_not_sent_and_sent_is_never_hidden(self):
        rows = self.rows([
            {"date": "2026-11-26", "key": "u", "status": "submitted_unconfirmed"},
            {"date": "2026-11-26", "key": "s", "status": "submitted"},
            {"date": "2026-11-27", "key": "s", "status": "needs_you"},  # a re-run after it went out
        ])
        self.assertEqual(rows["u"]["group"], "unconfirmed")
        self.assertEqual(rows["s"]["group"], "sent")

    def test_screenshot_names_are_plain_png_files_only(self):
        rows = self.rows([
            {"date": "d", "key": "ok", "status": "blocked", "screenshot": "/x/profile/auto_apply/shots/2026-a.png"},
            {"date": "d", "key": "bad", "status": "blocked", "screenshot": "../../etc/passwd"},
        ])
        self.assertEqual(rows["ok"]["screenshot"], "2026-a.png")
        self.assertEqual(rows["bad"]["screenshot"], "")
        self.assertFalse(ta.SHOT_NAME.match("../x.png"))

    def test_no_log_no_rows(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(ta.applied_rows(Path(d)), [])


if __name__ == "__main__":
    unittest.main()
