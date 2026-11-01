#!/usr/bin/env python3
"""Prepare every opportunity at once: posting, tailored CV + PDF, recruiter emails, cold email.

Per job (best profile match first):
  1. read the posting once and keep it in profile/postings/ (LinkedIn pages
     are paced; unreadable pages fall back to the job title);
  2. tailor a CV from the profile and render it to PDF with the local Chrome
     (profile/tailored/<id>.pdf) - the file to attach;
  3. find the emails the company publishes (outreach.find_contacts), one
     website crawl per company however many jobs it has;
  4. when an address was found, draft a cold email with Claude (a few at a
     time), else leave the draft for the page to write on demand.
Everything lands in the per-job outreach record the Send list tab reads.
Jobs already prepared are skipped unless `force` is set.
"""

import base64
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import job_finder  # noqa: E402
import outreach  # noqa: E402
import profile_store as ps  # noqa: E402

CHROME_PATHS = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
]
LINKEDIN_PAUSE = 1.5  # seconds between LinkedIn page reads


def chrome_path() -> str | None:
    for name in ("google-chrome", "chromium", "chromium-browser", "chrome"):
        if found := shutil.which(name):
            return found
    return next((p for p in CHROME_PATHS if Path(p).is_file()), None)


def pdf_pages(path: Path) -> int:
    try:
        from pypdf import PdfReader
        return len(PdfReader(str(path)).pages)
    except Exception:
        return 0


def render_pdf(cv: dict, base: Path) -> Path | None:
    """profile/tailored/<id>.pdf - always one A4 page.

    Prints the CV with the local Chrome, counts the pages, and if it spills
    over steps through profile_store.fit_variants (least relevant lines
    dropped first, then tighter spacing) until it fits. The fitted CV is saved
    back, so the HTML preview, the PDF and the email attachment all match.
    None without a Chrome.
    """
    exe = chrome_path()
    if not exe:
        return None
    photo = None
    if cv.get("photo") and (base / "photo.png").is_file():
        photo = "data:image/png;base64," + base64.b64encode((base / "photo.png").read_bytes()).decode()
    out = base / "tailored" / f"{cv['id']}.pdf"
    with tempfile.TemporaryDirectory() as d:
        src, tmp = Path(d) / "cv.html", Path(d) / "cv.pdf"
        fitted, last_ok = None, None
        for variant in ps.fit_variants(cv):
            src.write_text(ps.render_cv(variant, photo), encoding="utf-8")
            tmp.unlink(missing_ok=True)
            try:
                subprocess.run([exe, "--headless=new", "--disable-gpu", "--no-pdf-header-footer",
                                f"--user-data-dir={d}/chrome", f"--print-to-pdf={tmp}", src.as_uri()],
                               capture_output=True, timeout=90)
            except (OSError, subprocess.TimeoutExpired):
                return None
            if not tmp.is_file() or tmp.stat().st_size < 1000:
                return None
            last_ok = variant
            if pdf_pages(tmp) <= 1:
                fitted = variant
                break
        fitted = fitted or last_ok  # the shortest variant if even that spills (keeps the PDF usable)
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(tmp), out)
    if fitted is not None and (fitted.get("fit_level") or fitted.get("density")) != (cv.get("fit_level") or cv.get("density")):
        ps.save_tailored(fitted, base)
    return out


def safe_key(key: str) -> str:
    return re.sub(r"[^a-z0-9_-]", "-", key.lower())[:120] or "job"


class Store:
    """Per-job outreach records and cached posting text under the profile folder."""

    def __init__(self, base: Path):
        self.base = base

    def record_path(self, key: str) -> Path:
        return self.base / "outreach" / f"{safe_key(key)}.json"

    def load(self, key: str) -> dict:
        p = self.record_path(key)
        try:
            return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def save(self, key: str, record: dict) -> None:
        ps._atomic_write(self.record_path(key), json.dumps(record, ensure_ascii=False, indent=2).encode("utf-8"))

    def all(self) -> dict:
        out = {}
        for p in (self.base / "outreach").glob("*.json"):
            try:
                r = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if r.get("key"):
                out[r["key"]] = r
        return out


def prepare_all(opps: list[dict], base: Path, progress=lambda msg: None, force: bool = False,
                polish_top: int = 0, drafts: bool = True, limit: int | None = None) -> dict:
    profile = ps.load_profile(base)
    if not ps.has_content(profile):
        raise ValueError("add your CV on the Profile tab first")
    store = Store(base)
    jobs = opps[:limit] if limit else opps
    stats = {"jobs": len(jobs), "cvs": 0, "pdfs": 0, "with_email": 0, "drafts": 0, "from_title": 0, "skipped": 0}
    company_contacts = {}  # company -> find_contacts result, one crawl per company
    to_draft = []

    for n, opp in enumerate(jobs, 1):
        rec = store.load(opp["key"])
        if rec.get("cv_id") and rec.get("pdf") and "contacts" in rec and not force:
            stats["skipped"] += 1
            stats["cvs"] += 1
            stats["pdfs"] += 1
            stats["with_email"] += bool(rec.get("contacts"))
            if drafts and rec.get("contacts") and not rec.get("draft"):
                to_draft.append(opp)
            continue
        progress(f"{n}/{len(jobs)} · {opp['company']} — reading the posting")

        # 1. posting (kept, so later runs and drafts reuse it)
        details = None
        if not rec.get("posting"):
            if "linkedin.com" in opp["url"]:
                time.sleep(LINKEDIN_PAUSE)
            details = job_finder.posting_details(opp["url"])
            rec["posting"] = details["text"][:20000]
            rec["company_url"] = details.get("company_url")
            if details.get("hiring_team"):
                rec["hiring_team"] = details["hiring_team"]
        posting = rec["posting"]
        from_title = len(posting.strip()) < 30
        stats["from_title"] += from_title

        # 2. CV + PDF - a CV the user already had polished is kept, not regenerated
        progress(f"{n}/{len(jobs)} · {opp['company']} — tailoring the CV")
        existing = next((ps.load_tailored(t["id"], base) for t in ps.list_tailored(base)
                         if t.get("opportunity") == opp["key"] and t.get("polished")), None)
        if existing and not force:
            cv = existing
        else:
            cv = ps.tailor(profile, posting if not from_title else f"{opp['title']}\n{opp['company']}",
                           opp["title"], opp["company"])
            cv["opportunity"] = {"key": opp["key"], "url": opp["url"], "portal": opp["portal"]}
            cv["from_title"] = from_title
            if polish_top and n <= polish_top:
                try:
                    cv = ps.polish(cv, profile)
                except ps.ClaudeError:
                    pass
            cv = ps.save_tailored(cv, base)
        stats["cvs"] += 1
        pdf = render_pdf(cv, base)
        stats["pdfs"] += bool(pdf)

        # 3. contacts, one crawl per company
        progress(f"{n}/{len(jobs)} · {opp['company']} — looking for a recruiting email")
        ckey = ps.fold(opp["company"]).strip()
        if ckey not in company_contacts:
            company_contacts[ckey] = outreach.find_contacts(
                opp, {"text": "", "emails": [], "company_url": rec.get("company_url"), "company": opp["company"]})
        found = company_contacts[ckey]
        # Addresses printed in this posting come first; then the company's own.
        posted = sorted(outreach.emails_in(posting) | outreach.spelled_emails(posting, opp["company"]))
        contacts = [{"email": e, "source": opp["url"], "kind": "posting"} for e in posted]
        contacts += [c for c in found["contacts"] if c["email"] not in posted]
        stats["with_email"] += bool(contacts)

        rec.update(key=opp["key"], company=opp["company"], title=opp["title"], url=opp["url"], portal=opp["portal"],
                   match=opp.get("match"), cv_id=cv["id"], coverage=cv.get("coverage"), from_title=from_title,
                   pdf=f"tailored/{cv['id']}.pdf" if pdf else None, contacts=contacts, site=found.get("site"),
                   site_note=found.get("site_note"), prepared=time.strftime("%Y-%m-%d"))
        if force:
            rec.pop("draft", None)
        store.save(opp["key"], rec)
        if drafts and contacts and not rec.get("draft"):
            to_draft.append(opp)

    # 4. cold emails for jobs with an address, three Claude calls at a time
    if drafts and to_draft and ps.claude_path():
        progress(f"Writing {len(to_draft)} cold emails…")
        done = [0]

        def draft_one(opp):
            rec = store.load(opp["key"])
            cv = ps.load_tailored(rec["cv_id"], base) if rec.get("cv_id") else None
            d = outreach.draft_email(profile, opp, rec.get("posting") or "", rec["contacts"][0]["email"], cv)
            rec["draft"] = d
            store.save(opp["key"], rec)
            done[0] += 1
            progress(f"Writing cold emails… {done[0]}/{len(to_draft)}")
            return d

        with ThreadPoolExecutor(max_workers=3) as pool:
            for _ in pool.map(draft_one, to_draft):
                stats["drafts"] += 1
    return stats
