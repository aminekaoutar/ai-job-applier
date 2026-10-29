#!/usr/bin/env python3
"""Profile-driven job search and posting fetches for the tracker app.

Free-Work is queried straight through its public JSON API (the endpoints
`.agents/skills/free-work-search/url-reference.md` documents), so this works
without bun. LinkedIn goes through the `linkedin-search` CLI and runs only when
`bun` is installed. Results are stored through `scrape_state.run`, the same
dedupe-and-store path `/scrape` uses, with a fit label from the profile match.
"""

import argparse
import contextlib
import html
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import profile_store as ps  # noqa: E402
import scrape_state  # noqa: E402
from job_key import make_key  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
FW_API = "https://www.free-work.com/api"
UA = "Mozilla/5.0 (compatible; ai-job-search-tracker/1.0)"
LOCATIONS = {"paris": "fr~ile-de-france~paris~", "ile-de-france": "fr~ile-de-france~~", "france": "fr~~~"}
LINKEDIN_LOCATIONS = {"paris": "Paris, Île-de-France, France", "ile-de-france": "Île-de-France, France", "france": "France"}


def find_bun() -> str | None:
    found = shutil.which("bun")
    if found:
        return found
    home = Path.home() / ".bun/bin/bun"
    return str(home) if home.is_file() else None


def _get_json(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/ld+json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode("utf-8"))


def html_text(markup: str) -> str:
    """Visible text of a posting's HTML body, one block per line."""
    markup = re.sub(r"(?is)<(script|style|template)\b.*?</\1\s*>|<!--.*?-->", "", markup or "")
    markup = re.sub(r"(?i)<li\b[^>]*>", "\n- ", markup)
    markup = re.sub(r"(?i)<br\s*/?>|</(p|li|ul|ol|div|h\d)>", "\n", markup)
    text = html.unescape(re.sub(r"<[^>]+>", " ", markup))
    lines = [re.sub(r"\s+", " ", l).strip() for l in text.splitlines()]
    return "\n".join(l for l in lines if l and l != "-")


def freework_search(query: str, location: str = "paris", page: int = 1, jobage: int = 30) -> list[dict]:
    params = {"searchKeywords": query, "itemsPerPage": "20", "page": str(page)}
    if location in LOCATIONS:
        params["locationKeys"] = LOCATIONS[location]
    since = {1: "less_than_24_hours", 7: "less_than_7_days", 14: "less_than_14_days", 30: "less_than_30_days"}.get(jobage)
    if since:
        params["publishedSince"] = since
    data = _get_json(f"{FW_API}/job_postings?{urllib.parse.urlencode(params)}")
    out = []
    for r in data.get("hydra:member", []):
        loc = r.get("location") or {}
        out.append({
            "title": r.get("title") or "", "company": (r.get("company") or {}).get("name") or "",
            "url": f"https://www.free-work.com/fr/tech-it/{(r.get('job') or {}).get('slug') or 'emploi'}/job-mission/{r.get('slug')}",
            "location": loc.get("label") or loc.get("locality"), "date": (r.get("publishedAt") or "")[:10] or None,
            "skills": " ".join(s.get("name") or "" for s in r.get("skills") or []),
            "portal": "free-work-search",
        })
    return out


def linkedin_search(query: str, location: str = "paris", page: int = 1, jobage: int = 30) -> list[dict]:
    bun = find_bun()
    if not bun:
        return []
    cli = ROOT / ".agents/skills/linkedin-search/cli/src/cli.ts"
    cmd = [bun, "run", str(cli), "search", "-q", query, "-l", LINKEDIN_LOCATIONS.get(location, "France"),
           "--jobage", str(jobage), "--page", str(page), "--format", "json"]
    done = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if done.returncode != 0:
        return []
    return [{**r, "portal": "linkedin-search", "skills": ""} for r in json.loads(done.stdout).get("results", [])]


def fetch_posting(url: str) -> str:
    """Full text of a Free-Work or LinkedIn posting, or "" when it cannot be fetched."""
    m = re.match(r"https://www\.free-work\.com/.*/job-mission/([a-z0-9-]+)/?$", url or "")
    if m:
        try:
            r = _get_json(f"{FW_API}/job_postings/{m.group(1)}")
        except Exception:
            return ""
        parts = [r.get("title") or "", html_text(r.get("description") or ""),
                 html_text(r.get("candidateProfile") or ""), html_text(r.get("companyDescription") or ""),
                 "Skills: " + ", ".join(s.get("name") or "" for s in r.get("skills") or [])]
        return "\n\n".join(p for p in parts if p.strip())
    m = re.match(r"https://(?:[a-z]+\.)?linkedin\.com/jobs/view/(?:[^/]*-)?(\d{6,})", url or "")
    bun = find_bun()
    if m and bun:
        cli = ROOT / ".agents/skills/linkedin-search/cli/src/cli.ts"
        done = subprocess.run([bun, "run", str(cli), "detail", m.group(1), "--format", "json"],
                              capture_output=True, text=True, timeout=120)
        if done.returncode == 0:
            d = json.loads(done.stdout)
            return f"{d.get('title') or ''}\n\n{d.get('description') or ''}"
    return ""


def fit_label(score: int) -> str:
    return "high" if score >= 60 else "medium" if score >= 30 else "low"


def profile_search(profile: dict, seen: Path, tracker: Path, location: str = "paris",
                   pages: int = 2, progress=lambda msg: None) -> dict:
    """Search with keywords from the profile, score each hit, store the new ones."""
    queries = ps.search_queries(profile)
    p_terms, targets = ps.profile_terms(profile), ps.target_words(profile)
    hits, errors = [], []
    use_linkedin = find_bun() is not None
    for i, q in enumerate(queries, 1):
        progress(f"Searching '{q}' ({i}/{len(queries)})")
        for page in range(1, pages + 1):
            try:
                res = freework_search(q, location, page)
            except Exception as exc:
                errors.append(f"Free-Work '{q}': {exc}")
                break
            hits += res
            if len(res) < 20:
                break
        if use_linkedin:
            for page in range(1, pages + 1):
                res = linkedin_search(q, location, page)
                hits += res
                time.sleep(1.5)  # keep LinkedIn volume low
                if len(res) < 10:
                    break

    today = date.today().isoformat()
    jobs, keys = [], set()
    for h in hits:
        if not (h.get("title") and h.get("company") and str(h.get("url", "")).startswith("http")):
            continue
        key = make_key(h["company"], h["title"], h["url"])
        if key in keys:
            continue
        keys.add(key)
        m = ps.match_job(p_terms, targets, h["title"], h.get("skills") or "")
        jobs.append({
            "title": h["title"], "company": h["company"], "url": h["url"], "location": h.get("location"),
            "first_seen": today, "posted_date": h.get("date"), "deadline": None,
            "fit": fit_label(m["score"]), "status": "new" if m["score"] >= 20 else "skipped",
            "portal": h["portal"], "source": "cli",
        })

    progress("Saving results")
    existing = set()
    if seen.is_file():
        try:
            doc = json.loads(seen.read_text(encoding="utf-8"))
            existing = set((doc.get("seen") if isinstance(doc, dict) and "seen" in doc else doc) or {})
        except (OSError, json.JSONDecodeError):
            pass
    jobs = [j for j in jobs if make_key(j["company"], j["title"], j["url"]) not in existing]
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as fh:
        json.dump(jobs, fh, ensure_ascii=False)
        tmp = fh.name
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            scrape_state.run(argparse.Namespace(command="store", results=Path(tmp), state=seen, tracker=tracker,
                                                portal=None, domain=None))
        stored = json.loads(buf.getvalue() or "{}")
    finally:
        os.unlink(tmp)
    added = stored.get("added", [])
    return {
        "queries": queries, "found": len(hits), "added": len(added),
        "relevant": sum(1 for j in jobs if j["status"] == "new" and make_key(j["company"], j["title"], j["url"]) in set(added)),
        "already_known": stored.get("already_seen", 0) + stored.get("already_applied", 0),
        "linkedin": use_linkedin, "errors": errors,
    }
