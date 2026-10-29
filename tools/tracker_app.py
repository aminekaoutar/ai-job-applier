#!/usr/bin/env python3
"""Local web interface for tracking applications: dashboard, pipeline, ghosting.

Serves one page on http://127.0.0.1 (never another interface) that reads and
edits the same files the commands use:

  job_search_tracker.csv       applications (written by /apply, /outcome, /gmail-sync)
  job_scraper/seen_jobs.json   jobs found by /scrape and scored by /rank (read-only here)

Classification follows /outcome's rules: days quiet count from the row's `date`
or the latest dated entry in `notes`, whichever is more recent; follow-ups are
the `followed up YYYY-MM-DD` markers in `notes`. An `applied` row quiet for the
ghost threshold (default 21 days, adjustable in the page) reads as ghosted; a
row the user resolved as `no_response` is ghosted for good.

Writes keep the tracker's shape: the existing header is preserved, no field is
ever quoted, and commas, double quotes and line breaks are stripped from
values (the rule tests/test_tracker_notes_csv_safe.py documents). Every status
change appends a dated note, so the timeline survives in the CSV itself.

Usage:
  python3 tools/tracker_app.py [--port 8765] [--no-browser]
  python3 tools/tracker_app.py --tracker PATH --seen PATH   (alternate files)
"""

import argparse
import base64
import csv
import hashlib
import json
import os
import re
import secrets
import sys
import tempfile
import threading
import webbrowser
from datetime import date
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))
from rank_state import STATE, TRACKER, norm  # noqa: E402
import github_import  # noqa: E402
import job_finder  # noqa: E402
import profile_store as ps  # noqa: E402

PAGE = Path(__file__).resolve().parent / "tracker_app.html"

HEADER = [
    "date", "company", "sector", "role", "role_type", "channel", "status",
    "contact_person", "fit_rating", "notes", "cv_file", "cover_letter_file",
    "source", "deadline",
]

# /outcome's Tracker status vocabulary. Legacy space spellings are read, never written.
STATUSES = ("drafted", "applied", "interview", "offer", "hired", "rejected",
            "no_response", "offer_declined", "withdrawn")
LEGACY = {"no response": "no_response", "offer declined": "offer_declined"}
FINAL = {"hired", "rejected", "no_response", "offer_declined", "withdrawn"}
RESPONDED = {"interview", "offer", "hired", "rejected", "offer_declined"}

FOLLOWUP_DAYS = 10  # /outcome Step 2b default
MAX_FOLLOWUPS = 2
DEFAULT_GHOST_DAYS = 21

ISO_DATE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")
FOLLOWED_UP = re.compile(r"followed up (\d{4}-\d{2}-\d{2})")

PORTALS = (
    ("linkedin.com", "LinkedIn"),
    ("welcometothejungle.com", "Welcome to the Jungle"),
    ("indeed.", "Indeed"),
    ("hellowork.com", "HelloWork"),
    ("free-work.com", "Free-Work"),
    ("apec.fr", "APEC"),
    ("francetravail.fr", "France Travail"),
    ("jobteaser.com", "JobTeaser"),
    ("stationf.co", "Station F"),
)
PORTAL_KEYS = {
    "linkedin-search": "LinkedIn", "linkedin": "LinkedIn",
    "welcometothejungle": "Welcome to the Jungle", "wttj": "Welcome to the Jungle",
    "indeed": "Indeed", "hellowork": "HelloWork",
    "free-work-search": "Free-Work", "free-work": "Free-Work", "freework": "Free-Work",
    "freehire-search": "freehire", "freehire": "freehire",
}


# --- tracker file -----------------------------------------------------------

def clean_field(value) -> str:
    """A value safe to write unquoted: no commas, double quotes or line breaks."""
    text = str(value if value is not None else "")
    text = re.sub(r"[\r\n]+", " ", text).replace('"', "").replace(",", ";")
    return text.strip()


def read_tracker(path: Path) -> tuple[list[str], list[dict], str]:
    """Return (header, rows, version). A missing file is an empty tracker."""
    if not path.is_file():
        return list(HEADER), [], ""
    raw = path.read_bytes()
    version = hashlib.sha256(raw).hexdigest()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("cp1252")  # a spreadsheet's plain "CSV" save
    reader = csv.reader(text.splitlines())
    header = next(reader, None) or list(HEADER)
    header = [h.strip() for h in header]
    rows = []
    for values in reader:
        if not any(v.strip() for v in values):
            continue
        # Rows from before `deadline` existed have one field fewer: pad, never drop.
        row = {h: (values[i].strip() if i < len(values) else "") for i, h in enumerate(header)}
        rows.append(row)
    return header, rows, version


def write_tracker(path: Path, header: list[str], rows: list[dict]) -> str:
    """Atomically rewrite the tracker under its existing header; return the new version."""
    lines = [",".join(header)]
    for row in rows:
        lines.append(",".join(clean_field(row.get(h, "")) for h in header))
    data = ("\n".join(lines) + "\n").encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tracker-", suffix=".csv")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return hashlib.sha256(data).hexdigest()


# --- classification -----------------------------------------------------------

def canonical_status(value: str) -> str:
    v = (value or "").strip().lower()
    return LEGACY.get(v, v)


def parse_day(value: str):
    try:
        return date.fromisoformat((value or "").strip()[:10])
    except ValueError:
        return None


def portal_of(source: str, channel: str = "") -> str:
    host = urlsplit(source).netloc.lower() if (source or "").startswith("http") else ""
    for needle, name in PORTALS:
        if needle in host:
            return name
    if host:
        return host.removeprefix("www.")
    return PORTAL_KEYS.get((channel or "").strip().lower(), (channel or "").strip() or "Other")


def derive(row: dict, today: date, ghost_days: int) -> dict:
    status = canonical_status(row.get("status", ""))
    notes = row.get("notes", "")
    applied_on = parse_day(row.get("date", ""))
    note_days = [d for d in (parse_day(m) for m in ISO_DATE.findall(notes)) if d]
    last = max([d for d in [applied_on, *note_days] if d], default=None)
    days_quiet = (today - last).days if last else None
    followups = len(FOLLOWED_UP.findall(notes))
    reached_interview = status in {"interview", "offer", "hired", "offer_declined"} or bool(
        re.search(r"\binterview\b", notes, re.IGNORECASE)
    )

    if status == "drafted":
        response = "not_sent"
    elif status in RESPONDED:
        response = "responded"
    elif status == "withdrawn":
        response = "withdrawn"
    elif status == "no_response":
        response = "ghosted"
    elif status == "applied" and days_quiet is not None and days_quiet >= ghost_days:
        response = "ghosted"
    elif status == "applied":
        response = "waiting"
    else:
        response = "unknown"

    followup_due = (
        status == "applied"
        and days_quiet is not None
        and days_quiet >= FOLLOWUP_DAYS
        and followups < MAX_FOLLOWUPS
    )
    deadline = parse_day(row.get("deadline", ""))
    return {
        "status": status,
        "known_status": status in STATUSES,
        "final": status in FINAL,
        "response": response,
        "applied_on": applied_on.isoformat() if applied_on else None,
        "last_activity": last.isoformat() if last else None,
        "days_quiet": days_quiet,
        "followups": followups,
        "followup_due": followup_due,
        "reached_interview": reached_interview,
        "portal": portal_of(row.get("source", ""), row.get("channel", "")),
        "deadline_days": (deadline - today).days if deadline else None,
    }


# --- scraped opportunities ---------------------------------------------------------

def read_opportunities(path: Path, applied_pairs: set) -> list[dict]:
    """Jobs /scrape found and /rank scored that are not in the tracker yet."""
    if not path.is_file():
        return []
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    seen = doc.get("seen") if isinstance(doc, dict) and "seen" in doc else doc
    if not isinstance(seen, dict):
        return []
    out = []
    for key, e in seen.items():
        if not isinstance(e, dict) or e.get("status") not in ("new", "ranked", "unreachable"):
            continue
        if (norm(e.get("company")), norm(e.get("title"))) in applied_pairs:
            continue
        url = e.get("url") if isinstance(e.get("url"), str) else ""
        portal = PORTAL_KEYS.get(str(e.get("portal") or "").lower()) or portal_of(url, str(e.get("portal") or ""))
        out.append({
            "key": key,
            "title": e.get("title") or "",
            "company": e.get("company") or "",
            "location": e.get("location") if isinstance(e.get("location"), str) else "",
            "url": url if url.startswith("http") else "",
            "portal": portal,
            "first_seen": e.get("first_seen") or "",
            "posted_date": e.get("posted_date") or "",
            "deadline": e.get("deadline") or "",
            "status": e.get("status"),
            "fit": e.get("fit") or "",
            "score": e.get("rank_score"),
            "verdict": e.get("rank_verdict") or "",
        })
    # Scored jobs first, best score first; unscored ones after, by /scrape's
    # quick fit (high, medium, low), newest posting first within each.
    scored = sorted((o for o in out if isinstance(o["score"], (int, float))), key=lambda o: -o["score"])
    unscored = [o for o in out if not isinstance(o["score"], (int, float))]
    unscored.sort(key=lambda o: str(o["posted_date"] or o["first_seen"]), reverse=True)
    unscored.sort(key=lambda o: {"high": 0, "medium": 1}.get(o["fit"], 2))
    return scored + unscored


def snapshot(tracker: Path, seen: Path, today: date, ghost_days: int, profile_dir: Path | None = None) -> dict:
    header, rows, version = read_tracker(tracker)
    apps = []
    for i, row in enumerate(rows):
        apps.append({"index": i, "row": {h: row.get(h, "") for h in HEADER}, **derive(row, today, ghost_days)})
    pairs = {(norm(r.get("company")), norm(r.get("role"))) for r in rows if norm(r.get("company"))}
    return {
        "today": today.isoformat(),
        "ghost_days": ghost_days,
        "followup_days": FOLLOWUP_DAYS,
        "statuses": list(STATUSES),
        "version": version,
        "tracker_exists": tracker.is_file(),
        "applications": apps,
        "opportunities": with_match(read_opportunities(seen, pairs), profile_dir),
    }


def with_match(opps: list[dict], profile_dir: Path | None) -> list[dict]:
    """Score each opportunity's title against the profile (best first) and link CVs built for it."""
    if profile_dir:
        cvs = {}
        for cv in reversed(ps.list_tailored(profile_dir)):  # oldest first, so the newest wins
            if cv.get("opportunity"):
                cvs[cv["opportunity"]] = {"id": cv["id"], "coverage": cv["coverage"]}
        for o in opps:
            o["cv"] = cvs.get(o["key"])
    profile = ps.load_profile(profile_dir) if profile_dir else None
    if not profile or not ps.has_content(profile):
        return opps
    terms, targets = ps.profile_terms(profile), ps.target_words(profile)
    for o in opps:
        m = ps.match_job(terms, targets, o["title"])
        o["match"], o["match_terms"] = m["score"], m["matched"]
    return sorted(opps, key=lambda o: (-(o.get("match") or 0), -(o["score"] or 0)))


# --- edits ---------------------------------------------------------------------

class Conflict(Exception):
    """The tracker changed on disk since the page loaded it."""


def apply_edit(tracker: Path, version: str, action: str, payload: dict, today: date) -> str:
    header, rows, current = read_tracker(tracker)
    if version != current:
        raise Conflict("the tracker changed since this page loaded it - reload and retry")
    stamp = today.isoformat()

    def target() -> dict:
        i = payload.get("index")
        if not isinstance(i, int) or not 0 <= i < len(rows):
            raise ValueError("no such application")
        row = rows[i]
        # Guard against a row that moved: the page names the row it means.
        if norm(row.get("company")) != norm(payload.get("company")) or norm(row.get("role")) != norm(payload.get("role")):
            raise Conflict("that application is no longer at this position - reload and retry")
        return row

    def add_note(row: dict, note: str) -> None:
        note = clean_field(note)
        if note:
            row["notes"] = f"{row['notes']} | {note}" if row.get("notes") else note

    if action == "add":
        company, role = clean_field(payload.get("company")), clean_field(payload.get("role"))
        if not company or not role:
            raise ValueError("company and role are required")
        status = canonical_status(payload.get("status") or "applied")
        if status not in STATUSES:
            raise ValueError(f"unknown status {status!r}")
        row = {h: "" for h in header}
        row.update({
            "date": clean_field(payload.get("date")) or stamp,
            "company": company,
            "role": role,
            "sector": clean_field(payload.get("sector")),
            "channel": clean_field(payload.get("channel")) or "online",
            "status": status,
            "contact_person": clean_field(payload.get("contact_person")),
            "source": clean_field(payload.get("source")),
            "deadline": clean_field(payload.get("deadline")),
            "notes": clean_field(payload.get("notes")),
            "cv_file": clean_field(payload.get("cv_file")),
        })
        if row["date"] and not parse_day(row["date"]):
            raise ValueError("date must be YYYY-MM-DD")
        rows.append(row)
    elif action == "status":
        row = target()
        status = canonical_status(payload.get("status"))
        if status not in STATUSES:
            raise ValueError(f"unknown status {status!r}")
        if canonical_status(row.get("status")) != status:
            row["status"] = status
            add_note(row, f"{status.replace('_', ' ')} {stamp}")
    elif action == "note":
        row = target()
        text = clean_field(payload.get("note"))
        if not text:
            raise ValueError("note is empty")
        add_note(row, f"{text} ({stamp})")
    elif action == "followup":
        row = target()
        add_note(row, f"followed up {stamp}")
    elif action == "contact":
        row = target()
        row["contact_person"] = clean_field(payload.get("contact_person"))
    else:
        raise ValueError(f"unknown action {action!r}")
    return write_tracker(tracker, header, rows)


# --- HTTP ------------------------------------------------------------------------

MAX_JSON = 64_000
MAX_UPLOAD_JSON = 15 * 1024 * 1024  # base64 of a 10 MB CV
UPLOAD_PATHS = {"/api/profile/import", "/api/profile/photo"}


class SearchJob:
    """The one profile search that may run at a time, polled by the page."""

    def __init__(self):
        self.lock = threading.Lock()
        self.state = {"running": False, "message": "", "result": None, "error": None}

    def start(self, fn) -> bool:
        with self.lock:
            if self.state["running"]:
                return False
            self.state = {"running": True, "message": "Starting", "result": None, "error": None}

        def progress(msg):
            with self.lock:
                self.state["message"] = msg

        def work():
            try:
                result = fn(progress)
                with self.lock:
                    self.state.update(running=False, message="Done", result=result)
            except Exception as exc:  # reported to the page, never swallowed
                with self.lock:
                    self.state.update(running=False, message="Failed", error=str(exc))

        threading.Thread(target=work, daemon=True).start()
        return True

    def status(self) -> dict:
        with self.lock:
            return dict(self.state)


def make_handler(tracker: Path, seen: Path, token: str, port_ref: dict, lock: threading.Lock,
                 profile_dir: Path = ps.PROFILE_DIR):
    search_job = SearchJob()
    profile_lock = threading.Lock()
    github_cache = {}  # username -> repos fetched by /api/github/repos

    def find_opportunity(key: str) -> dict:
        _, rows, _ = read_tracker(tracker)
        pairs = {(norm(r.get("company")), norm(r.get("role"))) for r in rows if norm(r.get("company"))}
        for o in read_opportunities(seen, pairs):
            if o["key"] == key:
                return o
        raise ValueError("that job is no longer in the opportunities list")

    def build_cv(profile: dict, posting: str, body: dict, opp: dict | None) -> dict:
        cv = ps.tailor(profile, posting, str(body.get("job_title") or (opp or {}).get("title") or ""),
                       str(body.get("company") or (opp or {}).get("company") or ""),
                       max_bullets=int(body.get("max_bullets") or 4), max_projects=int(body.get("max_projects") or 3),
                       include_photo=bool(body.get("include_photo", True)))
        if str(body.get("headline") or "").strip():
            cv["basics"]["headline"] = str(body["headline"]).strip()
        if opp:
            cv["opportunity"] = {"key": opp["key"], "url": opp["url"], "portal": opp["portal"]}
        if body.get("polish"):
            cv = ps.polish(cv, profile, int(body.get("max_bullets") or 4))
        return ps.save_tailored(cv, profile_dir)

    class Handler(BaseHTTPRequestHandler):
        server_version = "tracker-app"

        def log_message(self, fmt, *args):  # quiet by default
            pass

        def _host_ok(self) -> bool:
            # Refuse DNS-rebinding: only our own loopback names may address us.
            host = (self.headers.get("Host") or "").lower()
            port = port_ref["port"]
            return host in {f"127.0.0.1:{port}", f"localhost:{port}"}

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, obj) -> None:
            self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

        def _profile_payload(self) -> dict:
            profile = ps.load_profile(profile_dir)
            return {
                "profile": profile,
                "has_content": ps.has_content(profile),
                "claude_available": ps.claude_path() is not None,
                "bun_available": job_finder.find_bun() is not None,
                "queries": ps.search_queries(profile) if ps.has_content(profile) else [],
                "tailored": ps.list_tailored(profile_dir),
            }

        def do_GET(self):
            if not self._host_ok():
                return self._json(HTTPStatus.FORBIDDEN, {"error": "bad host"})
            url = urlsplit(self.path)
            qs = parse_qs(url.query)
            if url.path == "/":
                html = PAGE.read_text(encoding="utf-8").replace("__TRACKER_TOKEN__", token)
                return self._send(HTTPStatus.OK, html.encode("utf-8"), "text/html; charset=utf-8")
            if url.path == "/api/data":
                try:
                    ghost = int(qs.get("ghost_days", [DEFAULT_GHOST_DAYS])[0])
                except ValueError:
                    ghost = DEFAULT_GHOST_DAYS
                ghost = min(max(ghost, 1), 365)
                return self._json(HTTPStatus.OK, snapshot(tracker, seen, date.today(), ghost, profile_dir))
            if url.path == "/api/profile":
                return self._json(HTTPStatus.OK, self._profile_payload())
            if url.path == "/api/search/status":
                return self._json(HTTPStatus.OK, search_job.status())
            if url.path == "/profile/photo.png":
                photo = profile_dir / "photo.png"
                if not photo.is_file():
                    return self._json(HTTPStatus.NOT_FOUND, {"error": "no photo"})
                return self._send(HTTPStatus.OK, photo.read_bytes(), "image/png")
            if url.path.startswith("/cv/"):
                cv = ps.load_tailored(url.path[len("/cv/"):], profile_dir)
                if cv is None:
                    return self._json(HTTPStatus.NOT_FOUND, {"error": "no such CV"})
                photo = "/profile/photo.png" if cv.get("photo") and (profile_dir / "photo.png").is_file() else None
                return self._send(HTTPStatus.OK, ps.render_cv(cv, photo).encode("utf-8"), "text/html; charset=utf-8")
            return self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})

        def do_POST(self):
            if not self._host_ok():
                return self._json(HTTPStatus.FORBIDDEN, {"error": "bad host"})
            # A page on another origin cannot set this header without a CORS
            # preflight we never answer, and cannot read the token.
            if not secrets.compare_digest(self.headers.get("X-Tracker-Token", ""), token):
                return self._json(HTTPStatus.FORBIDDEN, {"error": "bad token"})
            path = urlsplit(self.path).path
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length > (MAX_UPLOAD_JSON if path in UPLOAD_PATHS else MAX_JSON):
                    raise ValueError("request too large")
                body = json.loads(self.rfile.read(length) or b"{}")
                if not isinstance(body, dict):
                    raise ValueError("expected a JSON object")
                return self._route(path, body)
            except Conflict as exc:
                return self._json(HTTPStatus.CONFLICT, {"error": str(exc)})
            except ps.ClaudeError as exc:
                return self._json(HTTPStatus.BAD_GATEWAY, {"error": f"Claude: {exc}"})
            except (ValueError, json.JSONDecodeError) as exc:
                return self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})

        def _route(self, path: str, body: dict):
            if path == "/api/edit":
                with lock:
                    version = apply_edit(tracker, str(body.get("version", "")), str(body.get("action", "")),
                                         body.get("payload") or {}, date.today())
                return self._json(HTTPStatus.OK, {"ok": True, "version": version})

            if path == "/api/profile/save":
                with profile_lock:
                    current = ps.load_profile(profile_dir)
                    incoming = body.get("profile") or {}
                    incoming["photo"] = current["photo"]
                    incoming["source"] = current["source"]
                    ps.save_profile(incoming, profile_dir)
                return self._json(HTTPStatus.OK, self._profile_payload())

            if path == "/api/profile/import":
                try:
                    data = base64.b64decode(str(body.get("data") or ""), validate=True)
                except ValueError as exc:
                    raise ValueError("upload was not readable") from exc
                method = "claude" if body.get("method") == "claude" else "local"
                with profile_lock:
                    result = ps.import_cv(str(body.get("filename") or "cv"), data, method, profile_dir)
                return self._json(HTTPStatus.OK, {**self._profile_payload(), "warnings": result["warnings"]})

            if path == "/api/profile/photo":
                if body.get("remove"):
                    with profile_lock:
                        ps.remove_photo(profile_dir)
                else:
                    try:
                        data = base64.b64decode(str(body.get("data") or ""), validate=True)
                    except ValueError as exc:
                        raise ValueError("upload was not readable") from exc
                    with profile_lock:
                        ps.set_photo(data, profile_dir)
                return self._json(HTTPStatus.OK, self._profile_payload())

            if path == "/api/job-text":
                url = str(body.get("url") or "")
                text = job_finder.fetch_posting(url)
                return self._json(HTTPStatus.OK, {"text": text})

            if path == "/api/cv/tailor":
                profile = ps.load_profile(profile_dir)
                if not ps.has_content(profile):
                    raise ValueError("add your CV on the Profile tab first")
                posting = str(body.get("posting") or "")
                if len(posting.strip()) < 30:
                    raise ValueError("paste the job description (or fetch it) first")
                opp = find_opportunity(str(body["opportunity"])) if body.get("opportunity") else None
                cv = build_cv(profile, posting, body, opp)
                return self._json(HTTPStatus.OK, {"cv": {k: v for k, v in cv.items() if k != "posting"},
                                                  "tailored": ps.list_tailored(profile_dir)})

            if path == "/api/cv/for-opportunity":
                profile = ps.load_profile(profile_dir)
                if not ps.has_content(profile):
                    raise ValueError("add your CV on the Profile tab first")
                opp = find_opportunity(str(body.get("key") or ""))
                posting = job_finder.fetch_posting(opp["url"])
                if len(posting.strip()) < 30:
                    # Indeed, Welcome to the Jungle, HelloWork: the page has to be pasted.
                    return self._json(HTTPStatus.OK, {"need_paste": True, "opportunity": opp})
                cv = build_cv(profile, posting, body, opp)
                return self._json(HTTPStatus.OK, {"cv": {k: v for k, v in cv.items() if k != "posting"},
                                                  "posting": posting, "tailored": ps.list_tailored(profile_dir)})

            if path == "/api/github/repos":
                user = github_import.username_from(str(body.get("user") or ""))
                try:
                    repos = github_import.fetch_repos(user, cache_dir=profile_dir / "cache")
                except github_import.GitHubError as exc:
                    raise ValueError(str(exc)) from exc
                github_cache[user.lower()] = repos
                mine = ps.load_profile(profile_dir)["projects"]
                return self._json(HTTPStatus.OK, {"user": user, "repos": [
                    {k: r[k] for k in ("repo", "name", "url", "description", "skills", "stars", "pushed", "has_content")}
                    | {"excerpt": r["readme"][:280],
                       "in_profile": github_import.existing_project(github_import.repo_match_text(r), r["url"], mine) is not None}
                    for r in repos]})

            if path == "/api/github/import":
                user = github_import.username_from(str(body.get("user") or ""))
                repos = github_cache.get(user.lower())
                if repos is None:
                    raise ValueError("load the repositories first")
                wanted = set(body.get("repos") or [])
                chosen = [r for r in repos if r["repo"] in wanted]
                if not chosen:
                    raise ValueError("pick at least one repository")
                with profile_lock:
                    profile = ps.load_profile(profile_dir)
                    language = ps.detect_language(ps.profile_text(profile)) if ps.has_content(profile) else "French"
                    projects, warnings = github_import.to_projects(chosen, body.get("method") == "claude", language)
                    profile, counts = github_import.merge_into_profile(profile, projects, user)
                    ps.save_profile(profile, profile_dir)
                return self._json(HTTPStatus.OK, {**self._profile_payload(), "github": counts, "warnings": warnings})

            if path == "/api/cv/delete":
                cv_id = str(body.get("id") or "")
                if ps.load_tailored(cv_id, profile_dir) is None:
                    raise ValueError("no such CV")
                (profile_dir / "tailored" / f"{cv_id}.json").unlink()
                return self._json(HTTPStatus.OK, {"tailored": ps.list_tailored(profile_dir)})

            if path == "/api/search/start":
                profile = ps.load_profile(profile_dir)
                if not ps.has_content(profile):
                    raise ValueError("add your CV on the Profile tab first")
                location = str(body.get("location") or "paris")
                if location not in job_finder.LOCATIONS:
                    raise ValueError("location must be paris, ile-de-france or france")
                started = search_job.start(lambda progress: job_finder.profile_search(
                    profile, seen, tracker, location, pages=2, progress=progress))
                return self._json(HTTPStatus.OK if started else HTTPStatus.CONFLICT,
                                  {"started": started, **search_job.status()})

            return self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    return Handler


def serve(tracker: Path, seen: Path, port: int, open_browser: bool, profile_dir: Path = ps.PROFILE_DIR) -> None:
    token = secrets.token_urlsafe(24)
    port_ref = {"port": port}
    handler = make_handler(tracker, seen, token, port_ref, threading.Lock(), profile_dir)
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    port_ref["port"] = server.server_address[1]
    url = f"http://127.0.0.1:{port_ref['port']}/"
    print(f"Application tracker running at {url}  (Ctrl+C to stop)")
    print(f"  tracker: {tracker}")
    print(f"  scraped jobs: {seen}")
    print(f"  profile: {profile_dir}")
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--tracker", type=Path, default=TRACKER)
    p.add_argument("--seen", type=Path, default=STATE)
    p.add_argument("--profile-dir", type=Path, default=ps.PROFILE_DIR)
    p.add_argument("--no-browser", action="store_true")
    args = p.parse_args(argv)
    serve(args.tracker, args.seen, args.port, not args.no_browser, args.profile_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
