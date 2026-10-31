#!/usr/bin/env python3
"""Schedule tools/daily.py every morning with macOS launchd (a per-user LaunchAgent).

  python3 tools/install_daily.py              install (08:30 every day)
  python3 tools/install_daily.py --at 07:45   another time
  python3 tools/install_daily.py --status     is it installed, when did it last run
  python3 tools/install_daily.py --run-now    start one run immediately through launchd
  python3 tools/install_daily.py --uninstall  remove it

The job runs as you, only while you are logged in; a run missed because the
Mac was asleep starts when it wakes. Output goes to ~/Library/Logs/ai-job-search-daily.log.
"""

import argparse
import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

LABEL = "com.aijobsearch.daily"
ROOT = Path(__file__).resolve().parent.parent
PLIST = Path.home() / "Library/LaunchAgents" / f"{LABEL}.plist"
LOG = Path.home() / "Library/Logs/ai-job-search-daily.log"


def domain() -> str:
    return f"gui/{os.getuid()}"


def launchctl(*args) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True)


def build_plist(hour: int, minute: int) -> dict:
    # launchd starts jobs with a bare PATH; claude, gh, pdftotext and Chrome helpers live in these.
    path = ":".join(dict.fromkeys([str(Path.home() / ".local/bin"), "/opt/homebrew/bin", "/usr/local/bin",
                                   str(Path(sys.executable).parent), "/usr/bin", "/bin", "/usr/sbin", "/sbin"]))
    return {
        "Label": LABEL,
        "ProgramArguments": [sys.executable, str(ROOT / "tools" / "daily.py")],
        "WorkingDirectory": str(ROOT),
        "StartCalendarInterval": {"Hour": hour, "Minute": minute},
        "EnvironmentVariables": {"PATH": path, "PYTHONUNBUFFERED": "1"},
        "StandardOutPath": str(LOG),
        "StandardErrorPath": str(LOG),
        "ProcessType": "Background",
    }


def install(at: str) -> int:
    if sys.platform != "darwin":
        print("This scheduler is for macOS. On Linux, add to crontab:\n"
              f"  30 8 * * * cd {ROOT} && {sys.executable} tools/daily.py")
        return 1
    hour, minute = (int(x) for x in at.split(":"))
    PLIST.parent.mkdir(parents=True, exist_ok=True)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    launchctl("bootout", f"{domain()}/{LABEL}")  # replace any earlier version
    with open(PLIST, "wb") as fh:
        plistlib.dump(build_plist(hour, minute), fh)
    r = launchctl("bootstrap", domain(), str(PLIST))
    if r.returncode != 0:
        print(f"launchctl bootstrap failed: {r.stderr.strip() or r.stdout.strip()}")
        return 1
    print(f"Installed: tools/daily.py runs every day at {hour:02d}:{minute:02d}.")
    print(f"  plist: {PLIST}\n  log:   {LOG}")
    if str(ROOT).startswith(str(Path.home() / "Downloads")) or str(ROOT).startswith(str(Path.home() / "Documents")):
        print("  Note: the repo is under ~/Downloads or ~/Documents. If the log shows 'Operation not permitted',\n"
              "  allow Python in System Settings > Privacy & Security > Files and Folders (or Full Disk Access).")
    return 0


def uninstall() -> int:
    launchctl("bootout", f"{domain()}/{LABEL}")
    PLIST.unlink(missing_ok=True)
    print("Removed the daily run.")
    return 0


def status() -> int:
    r = launchctl("print", f"{domain()}/{LABEL}")
    if r.returncode != 0:
        print("Not installed.")
        return 1
    keep = [l.strip() for l in r.stdout.splitlines() if any(k in l for k in ("state =", "last exit code", "runs ="))]
    print(f"Installed ({PLIST}).\n  " + "\n  ".join(keep))
    if LOG.is_file():
        print("Last log lines:\n  " + "\n  ".join(LOG.read_text(encoding="utf-8", errors="replace").splitlines()[-5:]))
    return 0


def run_now() -> int:
    r = launchctl("kickstart", f"{domain()}/{LABEL}")
    print("Started." if r.returncode == 0 else f"Could not start: {r.stderr.strip()}")
    return r.returncode


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--at", default="08:30", help="HH:MM, local time (default 08:30)")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--uninstall", action="store_true")
    g.add_argument("--status", action="store_true")
    g.add_argument("--run-now", action="store_true")
    args = p.parse_args(argv)
    if args.uninstall:
        return uninstall()
    if args.status:
        return status()
    if args.run_now:
        return run_now()
    return install(args.at)


if __name__ == "__main__":
    sys.exit(main())
