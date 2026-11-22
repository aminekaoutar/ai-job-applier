#!/usr/bin/env python3
"""Carry your job-site logins from your everyday Google Chrome into auto-apply's browser.

  python3 tools/chrome_logins.py            (copy the job sites' cookies)
  python3 tools/chrome_logins.py --check    (only count them, change nothing)

Chrome (136+) refuses to be automated on your own profile, so auto_apply.py
runs Chrome on its own profile (profile/browser/). This copies the cookie rows
of the job sites only (HelloWork, Welcome to the Jungle, Free-Work, France
Travail) from your Chrome profile into that one. Rows are copied exactly as
stored, still encrypted: they are never decrypted or read here; Chrome itself
decrypts them with your macOS keychain, as it does for you. Nothing from any
other site is copied, and never LinkedIn's.

Both Chromes must be the same Google Chrome (auto_apply uses channel="chrome").
auto-apply's browser must be closed while this runs.
"""

import argparse
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHROME_DIR = Path.home() / "Library/Application Support/Google/Chrome"
JOB_SITES = ("hellowork.com", "welcometothejungle.com", "free-work.com", "francetravail.fr")


def _site_clause(sites) -> tuple[str, list]:
    parts, args = [], []
    for s in sites:
        parts.append("(host_key = ? OR host_key = ? OR host_key LIKE ?)")
        args += [s, "." + s, "%." + s]
    return " OR ".join(parts), args


def _columns(con: sqlite3.Connection) -> list[str]:
    return [r[1] for r in con.execute("PRAGMA table_info(cookies)")]


def _meta_version(con: sqlite3.Connection) -> str | None:
    try:
        row = con.execute("SELECT value FROM meta WHERE key = 'version'").fetchone()
    except sqlite3.Error:
        return None
    return row[0] if row else None


def count(db: Path, sites=JOB_SITES) -> dict:
    with tempfile.TemporaryDirectory() as t:
        copy = Path(t) / "Cookies"
        shutil.copy2(db, copy)  # Chrome keeps the live file locked
        con = sqlite3.connect(copy)
        try:
            return {s: con.execute(f"SELECT count(*) FROM cookies WHERE {_site_clause([s])[0]}",
                                   _site_clause([s])[1]).fetchone()[0] for s in sites}
        finally:
            con.close()


def copy_cookies(src_db: Path, dest_db: Path, sites=JOB_SITES) -> dict:
    """Replace dest's rows for `sites` with src's. Returns rows copied per site."""
    if "linkedin.com" in sites:
        raise ValueError("LinkedIn is never copied")
    where, args = _site_clause(sites)
    with tempfile.TemporaryDirectory() as t:
        src_copy = Path(t) / "Cookies"
        shutil.copy2(src_db, src_copy)
        src = sqlite3.connect(src_copy)
        dest = sqlite3.connect(dest_db)
        try:
            sv, dv = _meta_version(src), _meta_version(dest)
            if sv and dv and sv != dv:
                raise RuntimeError(f"cookie database versions differ (yours {sv}, auto-apply's {dv}): "
                                   "update Chrome, open and close auto-apply's browser once, then retry")
            cols = [c for c in _columns(src) if c in set(_columns(dest))]
            rows = src.execute(f"SELECT {', '.join(cols)} FROM cookies WHERE {where}", args).fetchall()
            with dest:
                dest.execute(f"DELETE FROM cookies WHERE {where}", args)
                dest.executemany(f"INSERT OR REPLACE INTO cookies ({', '.join(cols)}) "
                                 f"VALUES ({', '.join('?' * len(cols))})", rows)
            host_i = cols.index("host_key")
            out = {s: 0 for s in sites}
            for r in rows:
                for s in sites:
                    if r[host_i].lstrip(".") == s or r[host_i].endswith("." + s):
                        out[s] += 1
            return out
        finally:
            src.close()
            dest.close()


def browser_running(browser_dir: Path) -> bool:
    """Chrome's SingletonLock is a symlink to '<host>-<pid>'; a lock whose process is gone is stale."""
    lock = browser_dir / "SingletonLock"
    if not lock.is_symlink() and not lock.exists():
        return False
    try:
        pid = int(os.readlink(lock).rsplit("-", 1)[1])
        os.kill(pid, 0)
    except (OSError, ValueError, IndexError):
        return False
    return True


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--check", action="store_true", help="only count the job-site cookies, change nothing")
    p.add_argument("--chrome-profile", default="Default", help="your Chrome profile folder (default: Default)")
    p.add_argument("--browser-dir", type=Path, default=ROOT / "profile" / "browser")
    args = p.parse_args(argv)

    src = CHROME_DIR / args.chrome_profile / "Cookies"
    dest = args.browser_dir / "Default" / "Cookies"
    if not src.is_file():
        sys.exit(f"No Chrome cookies at {src}")
    have = count(src)
    print("Your Chrome - cookies per job site:", have)
    if args.check:
        return 0
    if not dest.is_file():
        sys.exit("auto-apply's browser has no profile yet: run `python3 tools/auto_apply.py --login` once, "
                 "close the window, then retry")
    if browser_running(args.browser_dir):
        sys.exit("auto-apply's browser is open - close its window first")
    copied = copy_cookies(src, dest)
    print("Copied into auto-apply's browser:", copied)
    missing = [s for s, n in have.items() if not n]
    if missing:
        print("Not logged in on these in your Chrome (log in there, then rerun):", ", ".join(missing))
    return 0


if __name__ == "__main__":
    sys.exit(main())
