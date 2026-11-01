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


LI_SEARCH = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"


def linkedin_search_py(query: str, location: str = "paris", page: int = 1, jobage: int = 30) -> list[dict]:
    """LinkedIn's public guest job search - the endpoint the linkedin-search CLI reads, parsed the same way."""
    params = {"keywords": query, "location": LINKEDIN_LOCATIONS.get(location, "France"),
              "start": str((page - 1) * 10)}
    if jobage and jobage < 9999:
        params["f_TPR"] = f"r{jobage * 86400}"
    try:
        markup = _get_text(f"{LI_SEARCH}?{urllib.parse.urlencode(params)}", LINKEDIN_UA)
    except Exception:
        return []
    out = []
    for chunk in markup.split('data-entity-urn="urn:li:jobPosting:')[1:]:
        m = re.match(r"(\d+)", chunk)
        title = re.search(r'class="base-search-card__title"[^>]*>(.*?)</h3>', chunk, re.S)
        if not m or not title:
            continue
        link = re.search(r'class="base-card__full-link[^"]*"[^>]*href="([^"]+)"', chunk)
        company = re.search(r'class="base-search-card__subtitle"[^>]*>(.*?)</h4>', chunk, re.S)
        loc = re.search(r'class="job-search-card__location"[^>]*>(.*?)</span>', chunk, re.S)
        date = re.search(r'class="job-search-card__listdate[^"]*"[^>]*datetime="([^"]+)"', chunk)
        url = html.unescape(link.group(1)).split("?")[0] if link else f"https://www.linkedin.com/jobs/view/{m.group(1)}"
        out.append({"title": html_text(title.group(1)), "company": html_text(company.group(1)) if company else "",
                    "url": url, "location": html_text(loc.group(1)) if loc else None,
                    "date": date.group(1) if date else None, "portal": "linkedin-search", "skills": ""})
    return out


# HelloWork: its search URLs are disallowed by robots.txt, but its static keyword / job-title
# listing pages are allowed. These exist for Paris and the Île-de-France towns below (checked
# 2026-11-01); each lists ~20 current offers. Not every keyword has a page - unknown ones 404.
HW_BASE = "https://www.hellowork.com"
HW_PAGES = {
    "paris": ["mot-cle_ia-ville_paris-75000", "mot-cle_intelligence-artificielle-ville_paris-75000",
              "mot-cle_data-ville_paris-75000", "mot-cle_data-science-ville_paris-75000",
              "mot-cle_python-ville_paris-75000", "metier_data-scientist-ville_paris-75000",
              "metier_developpeur-python-ville_paris-75000"],
    "ile-de-france": ["mot-cle_ia-ville_courbevoie-92400", "mot-cle_ia-ville_levallois-perret-92300",
                      "mot-cle_ia-ville_velizy-villacoublay-78140", "mot-cle_ia-ville_elancourt-78990"],
}
HW_PAGES["france"] = HW_PAGES["paris"] + HW_PAGES["ile-de-france"] + [
    f"mot-cle_ia-ville_{c}" for c in ("lyon-69000", "toulouse-31000", "nantes-44000", "lille-59000", "bordeaux-33000")]
HW_PAGES["ile-de-france"] = HW_PAGES["paris"] + HW_PAGES["ile-de-france"]
AGE = re.compile(r"il y a\s+(\d+)\s+(minute|heure|jour|semaine|mois)", re.I)


def _age_to_date(text: str) -> str | None:
    from datetime import date, timedelta
    m = AGE.search(text or "")
    if not m:
        return date.today().isoformat() if re.search(r"aujourd|instant", text or "", re.I) else None
    n, unit = int(m.group(1)), m.group(2).lower()
    days = {"minute": 0, "heure": 0, "jour": n, "semaine": 7 * n, "mois": 30 * n}[unit]
    return (date.today() - timedelta(days=days)).isoformat()


def hellowork_listing(page: str) -> list[dict]:
    """Offers on one HelloWork listing page (robots.txt-checked)."""
    import robots_check
    url = f"{HW_BASE}/fr-fr/emploi/{page}.html"
    if robots_check.gate(url)[0] != 0:
        return []
    try:
        markup = _get_text(url)
    except Exception:
        return []
    out = []
    # Cards: <a ... href="/fr-fr/emplois/ID.html" title="..." data-cy="offerTitle"> <h3><span>TITLE</span><span>COMPANY</span></h3>
    for m in re.finditer(r'<a\b[^>]*href="(/fr-fr/emplois/\d+\.html)"[^>]*data-cy="offerTitle"[^>]*>(.*?)</a>', markup, re.S):
        href, inner = m.groups()
        rest = markup[m.end():m.end() + 3000]
        nxt = rest.find('data-cy="offerTitle"')
        rest = rest if nxt == -1 else rest[:nxt]
        spans = re.findall(r"<span[^>]*>(.*?)</span>", inner, re.S)
        if not spans:
            continue
        loc = re.search(r'data-cy="localisationCard"[^>]*>(.*?)</div>', rest, re.S)
        contract = re.search(r'data-cy="contractCard"[^>]*>(.*?)</div>', rest, re.S)
        out.append({
            "title": html_text(spans[0]), "company": html_text(spans[1]) if len(spans) > 1 else "",
            "url": HW_BASE + href, "location": html_text(loc.group(1)) if loc else None,
            "contract": html_text(contract.group(1)) if contract else None,
            "date": _age_to_date(rest), "portal": "hellowork", "skills": "",
        })
    return out


def hellowork_search(location: str = "paris", jobage: int = 30, pause: float = 1.5) -> list[dict]:
    from datetime import date, timedelta
    cutoff = (date.today() - timedelta(days=jobage)).isoformat() if jobage and jobage < 9999 else ""
    seen, out = set(), []
    for i, page in enumerate(HW_PAGES.get(location, HW_PAGES["paris"])):
        if i:
            time.sleep(pause)
        for job in hellowork_listing(page):
            if job["url"] in seen or (cutoff and job["date"] and job["date"] < cutoff):
                continue
            seen.add(job["url"])
            out.append(job)
    return out


def linkedin_search(query: str, location: str = "paris", page: int = 1, jobage: int = 30) -> list[dict]:
    bun = find_bun()
    if not bun:
        return linkedin_search_py(query, location, page, jobage)
    cli = ROOT / ".agents/skills/linkedin-search/cli/src/cli.ts"
    cmd = [bun, "run", str(cli), "search", "-q", query, "-l", LINKEDIN_LOCATIONS.get(location, "France"),
           "--jobage", str(jobage), "--page", str(page), "--format", "json"]
    done = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if done.returncode != 0:
        return []
    return [{**r, "portal": "linkedin-search", "skills": ""} for r in json.loads(done.stdout).get("results", [])]


LINKEDIN_UA = "Mozilla/5.0 (compatible; linkedin-search-cli/1.0)"
FW_COMPANY = re.compile(r"https://www\.free-work\.com/.*/job-mission/([a-z0-9-]+)/?$")
LI_JOB = re.compile(r"https://(?:[a-z]+\.)?linkedin\.com/jobs/view/(?:[^/?#]*-)?(\d{6,})")


def _get_text(url: str, ua: str = UA, timeout: int = 20) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": ua, "Accept": "text/html,application/xhtml+xml",
                                                "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        if "html" not in (r.headers.get("Content-Type") or "html"):
            return ""
        return r.read(2_000_000).decode(r.headers.get_content_charset() or "utf-8", "replace")


def _div_by_class(markup: str, cls: str) -> str | None:
    """Inner HTML of the first <div> carrying a class, counting nested divs."""
    m = re.search(r'<div[^>]*class="[^"]*' + re.escape(cls) + r'[^"]*"[^>]*>', markup)
    if not m:
        return None
    i, depth = m.end(), 1
    while depth and i < len(markup):
        o, c = markup.find("<div", i), markup.find("</div>", i)
        if c == -1:
            return None
        if o != -1 and o < c:
            depth, i = depth + 1, o + 4
        else:
            depth, i = depth - 1, c + 6
    return markup[m.end():i - 6]


def linkedin_hiring_team(markup: str) -> list[dict]:
    """People LinkedIn shows under "Meet the hiring team" on a public job page: name, headline, profile."""
    i = markup.find("message-the-recruiter")
    if i == -1:
        return []
    block = markup[i:i + 6000]
    people = []
    for m in re.finditer(r'href="(https://[a-z]{2,3}\.linkedin\.com/in/[^"?#]+)', block):
        chunk = block[m.end():m.end() + 1500]
        texts = [t for t in (html_text(x) for x in re.findall(r">([^<>]{2,120})<", chunk)) if t and len(t) > 1]
        name = texts[0] if texts else None
        headline = next((t for t in texts[1:] if t != name and not re.match(r"(?i)send|envoyer|message", t)), None)
        if name and m.group(1) not in {p["profile"] for p in people}:
            people.append({"name": name, "headline": headline, "profile": m.group(1)})
    return people[:3]


def linkedin_posting(job_id: str) -> dict:
    """LinkedIn's public guest job page - the endpoint the linkedin-search CLI reads."""
    html_ = _get_text(f"https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{job_id}", LINKEDIN_UA)
    body = _div_by_class(html_, "show-more-less-html__markup") or _div_by_class(html_, "description__text") or ""
    title = re.search(r'class="(?:top-card-layout__title|topcard__title)[^"]*"[^>]*>(.*?)</h[12]>', html_, re.S)
    company = re.search(r'class="topcard__org-name-link[^"]*"[^>]*>(.*?)</a>', html_, re.S)
    text = html_text(body)
    head = html_text(title.group(1)) if title else ""
    return {"text": f"{head}\n\n{text}".strip() if text else "", "company_url": None,
            "company": html_text(company.group(1)) if company else None, "emails": [],
            "hiring_team": linkedin_hiring_team(html_)}


def _jsonld_jobposting(markup: str) -> dict | None:
    # The type is sometimes HTML-escaped: application/ld&#x2B;json (HelloWork).
    for blk in re.findall(r'<script[^>]*type="application/ld(?:\+|&#x2B;|&#43;)json"[^>]*>(.*?)</script>', markup, re.S | re.I):
        try:
            data = json.loads(blk.strip())
        except json.JSONDecodeError:
            continue
        stack = data if isinstance(data, list) else [data]
        while stack:
            item = stack.pop()
            if isinstance(item, dict):
                if item.get("@type") == "JobPosting" or "JobPosting" in (item.get("@type") or []):
                    return item
                stack.extend(item.get("@graph") or [])
            elif isinstance(item, list):
                stack.extend(item)
    return None


def generic_posting(url: str) -> dict:
    """Any job page that embeds schema.org JobPosting data - only where robots.txt allows."""
    import robots_check
    code, _ = robots_check.gate(url)
    if code != 0:
        return {"text": "", "company_url": None, "emails": [], "blocked": True}
    try:
        markup = _get_text(url)
    except Exception:
        return {"text": "", "company_url": None, "emails": []}
    job = _jsonld_jobposting(markup)
    if not job:
        return {"text": "", "company_url": None, "emails": []}
    org = job.get("hiringOrganization") or {}
    org = org if isinstance(org, dict) else {}
    site = org.get("sameAs") or org.get("url")
    site = site[0] if isinstance(site, list) and site else site
    text = f"{job.get('title') or ''}\n\n{html_text(str(job.get('description') or ''))}".strip()
    return {"text": text, "company_url": site if isinstance(site, str) and site.startswith("http") else None,
            "company": org.get("name"), "emails": []}


def freework_posting(slug: str) -> dict:
    r = _get_json(f"{FW_API}/job_postings/{slug}")
    parts = [r.get("title") or "", html_text(r.get("description") or ""),
             html_text(r.get("candidateProfile") or ""), html_text(r.get("companyDescription") or ""),
             "Skills: " + ", ".join(s.get("name") or "" for s in r.get("skills") or [])]
    site = None
    company = r.get("company") or {}
    if company.get("slug"):
        try:
            site = _get_json(f"{FW_API}/companies/{company['slug']}").get("websiteUrl")
        except Exception:
            site = None
    contact = r.get("applicationContact")
    return {"text": "\n\n".join(p for p in parts if p.strip()), "company_url": site, "company": company.get("name"),
            "emails": [contact] if isinstance(contact, str) and "@" in contact else []}


def posting_details(url: str) -> dict:
    """{text, company_url, company, emails, source} for a posting URL; text is "" when unreadable."""
    try:
        if m := FW_COMPANY.match(url or ""):
            return {**freework_posting(m.group(1)), "source": "free-work"}
        if m := LI_JOB.match(url or ""):
            return {**linkedin_posting(m.group(1)), "source": "linkedin"}
        if (url or "").startswith("http"):
            return {**generic_posting(url), "source": "page"}
    except Exception:
        pass
    return {"text": "", "company_url": None, "company": None, "emails": [], "source": None}


def fetch_posting(url: str) -> str:
    """Full text of a posting, or "" when it cannot be fetched."""
    return posting_details(url)["text"]


def fit_label(score: int) -> str:
    return "high" if score >= 60 else "medium" if score >= 30 else "low"


def profile_search(profile: dict, seen: Path, tracker: Path, location: str = "paris",
                   pages: int = 2, progress=lambda msg: None, jobage: int = 30) -> dict:
    """Search with keywords from the profile, score each hit, store the new ones."""
    queries = ps.search_queries(profile)
    p_terms, targets = ps.profile_terms(profile), ps.target_words(profile)
    hits, errors = [], []
    use_linkedin = True  # the CLI with bun, else the same guest endpoint read in Python
    for i, q in enumerate(queries, 1):
        progress(f"Searching '{q}' ({i}/{len(queries)})")
        for page in range(1, pages + 1):
            try:
                res = freework_search(q, location, page, jobage)
            except Exception as exc:
                errors.append(f"Free-Work '{q}': {exc}")
                break
            hits += res
            if len(res) < 20:
                break
        if use_linkedin:
            for page in range(1, pages + 1):
                res = linkedin_search(q, location, page, jobage)
                hits += res
                time.sleep(2)  # keep LinkedIn volume low
                if len(res) < 10:
                    break

    progress("Reading HelloWork listings")
    try:
        hits += hellowork_search(location, jobage)
    except Exception as exc:
        errors.append(f"HelloWork: {exc}")

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
    new_keys = [j["key"] for j in jobs if j.get("key") in set(added)] if jobs and "key" in jobs[0] else list(added)
    return {
        "queries": queries, "found": len(hits), "added": len(added),
        "relevant": sum(1 for j in jobs if j["status"] == "new" and make_key(j["company"], j["title"], j["url"]) in set(added)),
        "already_known": stored.get("already_seen", 0) + stored.get("already_applied", 0),
        "linkedin": use_linkedin, "errors": errors, "new_keys": new_keys,
    }
