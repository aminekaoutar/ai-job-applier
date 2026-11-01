#!/usr/bin/env python3
"""The daily run: find new jobs for the profile and prepare each one to be sent.

  1. search Free-Work and LinkedIn with keywords taken from the profile
     (job_finder.profile_search), postings from the last DAYS days only;
  2. prepare every opportunity not prepared yet (batch.prepare_all): tailored
     CV + PDF, the company's published recruiting email, a drafted cold email;
  3. write profile/daily/<date>.json and show a macOS notification.

Nothing is ever sent or submitted: the drafts wait in the tracker's Send list
for the user to read and send from their own mailbox.

Run by hand:   python3 tools/daily.py [--dry-run]
Scheduled by:  python3 tools/install_daily.py   (macOS launchd, every morning)
"""

import argparse
import fcntl
import json
import os
import subprocess
import sys
import time
import traceback
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import batch  # noqa: E402
import gmail_drafts  # noqa: E402
import hunter  # noqa: E402
import job_finder  # noqa: E402
import profile_store as ps  # noqa: E402
import tracker_app as ta  # noqa: E402

DAYS = 2  # postings from the last two days: a missed morning is still covered


def daily_dir(base: Path) -> Path:
    d = base / "daily"
    d.mkdir(parents=True, exist_ok=True)
    return d


def log(base: Path, msg: str) -> None:
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}"
    print(line, flush=True)
    with open(daily_dir(base) / "daily.log", "a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def notify(title: str, text: str) -> None:
    """A macOS notification; silently skipped elsewhere."""
    if sys.platform != "darwin":
        return
    script = f'display notification {json.dumps(text)} with title {json.dumps(title)} sound name "Glass"'
    try:
        subprocess.run(["osascript", "-e", script], capture_output=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        pass


def opportunities(seen: Path, tracker: Path, base: Path) -> list[dict]:
    _, rows, _ = ta.read_tracker(tracker)
    pairs = {(ta.norm(r.get("company")), ta.norm(r.get("role"))) for r in rows if ta.norm(r.get("company"))}
    return ta.with_match(ta.read_opportunities(seen, pairs), base)


def run(base: Path = ps.PROFILE_DIR, seen: Path = ta.STATE, tracker: Path = ta.TRACKER,
        location: str = "paris", dry_run: bool = False) -> dict:
    lock_path = daily_dir(base) / ".lock"
    with open(lock_path, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log(base, "another daily run is still going - skipping")
            return {"skipped": True}

        started = time.time()
        profile = ps.load_profile(base)
        if not ps.has_content(profile):
            log(base, "no profile yet - import your CV in the tracker first")
            return {"error": "no profile"}

        log(base, f"searching ({', '.join(ps.search_queries(profile))})")
        found = job_finder.profile_search(profile, seen, tracker, location, pages=2, jobage=DAYS,
                                          progress=lambda m: None)
        log(base, f"search: {found['found']} results, {found['added']} new, {found['relevant']} relevant")

        todo = [o for o in opportunities(seen, tracker, base) if not batch.Store(base).load(o["key"]).get("pdf")]
        log(base, f"{len(todo)} jobs to prepare")
        stats = {"cvs": 0, "pdfs": 0, "with_email": 0, "drafts": 0}
        if todo and not dry_run:
            stats = batch.prepare_all(todo, base, progress=lambda m: None, drafts=True)

        found_people = 0
        if not dry_run and hunter.api_key():
            budget = int(json.loads((daily_dir(base) / "hunter.json").read_text()).get("max_per_day", 5)) \
                if (daily_dir(base) / "hunter.json").is_file() else 5
            store = batch.Store(base)
            want = sorted((r for r in store.all().values()
                           if r.get("cv_id") and not r.get("hunter") and not r.get("sent") and (r.get("match") or 0) >= 60
                           and not any(c.get("kind") in ("posting", "recruiter", "recruiting") for c in r.get("contacts") or [])),
                          key=lambda r: -(r.get("match") or 0))[:budget]
            for rec in want:
                try:
                    rec = hunter.enrich_record(rec, base / "cache")
                    store.save(rec["key"], rec)
                    found_people += rec["hunter"]["found"]
                except hunter.HunterError as exc:
                    log(base, f"hunter: {exc}")
                    if "quota" in str(exc).lower() or "key" in str(exc).lower():
                        break
            log(base, f"hunter: {len(want)} companies searched, {found_people} people found")

        gmail = None
        if not dry_run and gmail_drafts.load_config(base):
            try:
                gmail = gmail_drafts.push(base, (gmail_drafts.load_config(base) or {}).get("max_per_day", gmail_drafts.DEFAULT_MAX))
                log(base, f"gmail: {gmail['pushed']} drafts added, {gmail['left']} waiting")
            except gmail_drafts.GmailError as exc:
                gmail = {"error": str(exc)}
                log(base, f"gmail: {exc}")

        report = {
            "date": date.today().isoformat(), "finished": datetime.now().isoformat(timespec="seconds"),
            "minutes": round((time.time() - started) / 60, 1), "queries": found["queries"],
            "found": found["found"], "new_jobs": found["added"], "relevant": found["relevant"],
            "prepared": stats.get("cvs", 0), "pdfs": stats.get("pdfs", 0),
            "with_email": stats.get("with_email", 0), "drafts": stats.get("drafts", 0),
            "errors": found.get("errors", []),
            "gmail_drafts": (gmail or {}).get("pushed"), "gmail_error": (gmail or {}).get("error"),
            "hunter_people": found_people,
        }
        (daily_dir(base) / f"{report['date']}.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        log(base, f"done: {report}")
        where = f" · {report['gmail_drafts']} in Gmail drafts" if report.get("gmail_drafts") else ""
        notify("Job search - new today",
               f"{report['new_jobs']} new jobs · {report['prepared']} CVs ready · {report['drafts']} emails to send{where}")
        return report


def latest_report(base: Path = ps.PROFILE_DIR) -> dict | None:
    reports = sorted((base / "daily").glob("20*.json"))
    if not reports:
        return None
    try:
        return json.loads(reports[-1].read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--location", default="paris", choices=sorted(job_finder.LOCATIONS))
    p.add_argument("--dry-run", action="store_true", help="search only; prepare nothing")
    args = p.parse_args(argv)
    try:
        run(location=args.location, dry_run=args.dry_run)
    except Exception:
        log(ps.PROFILE_DIR, "FAILED\n" + traceback.format_exc())
        notify("Job search - daily run failed", "See profile/daily/daily.log")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
