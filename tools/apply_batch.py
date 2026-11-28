#!/usr/bin/env python3
"""Apply to N jobs: the best-matching prepared offers not tried yet, until N reach the employer.

  python3 tools/apply_batch.py 10            (apply until 10 are sent - one offer at a time)
  python3 tools/apply_batch.py 10 --list     (only show which offers would be tried, in order)

Offers come from the tracker's prepared jobs (profile/outreach/*.json), best match first. Skipped up
front, before any AI call: offers already tried (profile/auto_apply/log.jsonl), already sent, contract
types the candidate does not want (answers.json contract_wanted), and sites that cannot work yet
(LinkedIn and Free-Work need a login; Welcome to the Jungle and APEC block automated browsers).
Each offer runs tools/auto_apply.py --submit --headless (10-minute cap each). "Sent" counts the
statuses submitted and submitted_unconfirmed; up to 3 x N offers are tried, since some have closed.
The tracker app's "Apply to N jobs" button runs the same thing.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import threading
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))
import auto_apply as aa  # noqa: E402
import profile_store as ps  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SENT = {"submitted", "submitted_unconfirmed"}
# Not automatable today: login needed (LinkedIn, Free-Work, France Travail) or bot walls (WTTJ, APEC).
SKIP_HOSTS = ("linkedin.com", "free-work.com", "francetravail.fr", "welcometothejungle.com", "apec.fr")


def tried(profile_dir: Path) -> set[str]:
    """Keys and URLs of every offer auto-apply already ran on (dry runs excluded)."""
    out = set()
    try:
        lines = (profile_dir / "auto_apply" / "log.jsonl").read_text(encoding="utf-8").splitlines()
    except OSError:
        return out
    for line in lines:
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(e, dict) and e.get("status") != "dry_run":
            out.update(x for x in (e.get("key"), e.get("url")) if x)
    return out


def candidates(profile_dir: Path = ps.PROFILE_DIR) -> list[dict]:
    """Offers to try, best match first, with the reason each other offer was left out counted."""
    done = tried(profile_dir)
    wanted = str(aa.load_answers(profile_dir).get("contract_wanted") or "")
    rows = []
    for path in (profile_dir / "outreach").glob("*.json"):
        try:
            r = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        url = str(r.get("url") or "")
        host = (urlsplit(url).hostname or "").lower()
        if not url or r.get("sent") or r.get("key") in done or url in done:
            continue
        if any(host == h or host.endswith("." + h) for h in SKIP_HOSTS):
            continue
        if aa.unwanted_contract(str(r.get("title") or ""), str(r.get("posting") or ""), wanted):
            continue
        rows.append({"key": r.get("key", ""), "url": url, "company": r.get("company", ""),
                     "title": r.get("title", ""), "match": r.get("match") or 0, "portal": r.get("portal", "")})
    rows.sort(key=lambda r: -(r["match"] or 0))
    return rows


def run_one(url: str, submit: bool = True, timeout: int = 660) -> dict:
    """One headless auto_apply run (its own 10-minute cap; this timeout is only a safety net)."""
    cmd = [sys.executable, str(ROOT / "tools" / "auto_apply.py"), url, "--submit" if submit else "--no-submit", "--headless"]
    try:
        done = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return {"status": "error", "why": "gave up after 11 minutes"}
    for line in reversed(done.stdout.splitlines()):
        if line.startswith("RESULT "):
            try:
                return json.loads(line[7:])
            except json.JSONDecodeError:
                break
    return {"status": "error", "why": (done.stderr or done.stdout or "auto_apply failed").strip()[-300:]}


class Batch:
    """Apply until `target` are sent. State is readable while it runs (the tracker polls it)."""

    def __init__(self, target: int, profile_dir: Path = ps.PROFILE_DIR, runner=None, max_factor: int = 3):
        """runner(job) -> auto_apply's RESULT dict; default: run_one(job['url'])."""
        self.target, self.profile_dir = max(1, int(target)), profile_dir
        self.runner = runner or (lambda job: run_one(job["url"]))
        self.max_tries = self.target * max_factor
        self.sent, self.results, self.current, self.running, self.stopped = 0, [], None, False, False
        self.lock = threading.Lock()

    def status(self) -> dict:
        with self.lock:
            return {"target": self.target, "sent": self.sent, "tried": len(self.results), "running": self.running,
                    "current": self.current, "stopped": self.stopped, "results": list(self.results[-50:])}

    def stop(self) -> None:
        with self.lock:
            self.stopped = True

    def run(self, log=print) -> dict:
        with self.lock:
            self.running = True
        try:
            for job in candidates(self.profile_dir):
                with self.lock:
                    if self.stopped or self.sent >= self.target or len(self.results) >= self.max_tries:
                        break
                    self.current = {k: job[k] for k in ("company", "title", "url")}
                log(f"[{len(self.results) + 1}] {job['company']} - {job['title'][:60]}")
                res = self.runner(job)
                row = {**self.current, "status": res.get("status", "error"), "why": res.get("why", ""),
                       "secs": res.get("secs")}
                with self.lock:
                    self.results.append(row)
                    if row["status"] in SENT:
                        self.sent += 1
                log(f"    -> {row['status']}{(' - ' + row['why'][:100]) if row['why'] else ''}  ({self.sent}/{self.target} sent)")
        finally:
            with self.lock:
                self.running, self.current = False, None
        return self.status()


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("count", type=int, help="how many applications to send")
    p.add_argument("--list", action="store_true", help="only list the offers that would be tried")
    args = p.parse_args(argv)
    pool = candidates()
    if args.list:
        for i, r in enumerate(pool[: args.count * 3], 1):
            print(f"{i:3}. {r['match']:>3} | {r['company']} - {r['title'][:60]} | {r['url']}")
        print(f"{len(pool)} offers can be tried")
        return 0
    out = Batch(args.count).run()
    print(f"DONE: {out['sent']} sent out of {out['tried']} tried")
    return 0


if __name__ == "__main__":
    sys.exit(main())
