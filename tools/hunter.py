#!/usr/bin/env python3
"""Find the right person to email with Hunter.io (domain search).

For a company domain, Hunter lists professional addresses found on public
pages, each with a name, position, department, seniority, a confidence score
and its sources. This module asks for the HR / recruiting people first, then
the managers likely to hire for a tech role, and turns them into tracker
contacts ranked: recruiter > hiring manager > other.

Your own API key is used (https://hunter.io/api-keys): HUNTER_API_KEY, else
the macOS Keychain (service "ai-job-search-hunter"). Each domain search uses
one of your monthly search credits, so results are cached per domain for 30
days and the remaining quota is read from your account, never assumed.

  python3 tools/hunter.py --setup            store the API key (asks for it)
  python3 tools/hunter.py --quota            remaining searches this month
  python3 tools/hunter.py --domain acme.fr   try one domain
"""

import argparse
import getpass
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import profile_store as ps  # noqa: E402

API = "https://api.hunter.io/v2"
SERVICE = "ai-job-search-hunter"
CACHE_DAYS = 30
MIN_CONFIDENCE = 70  # below this Hunter is mostly guessing the pattern
RECRUIT = re.compile(r"recrut|recruit|talent|\bRH\b|\bHR\b|human resources|ressources humaines|people|acquisition|sourcing|staffing|chargée? de recrutement|campus", re.I)
MANAGER = re.compile(r"\b(cto|vp|head|director|directeur|directrice|manager|lead|chief|responsable|founder|fondateur|co-?founder|engineering|data|ai|ia|ml)\b", re.I)


class HunterError(RuntimeError):
    pass


def api_key() -> str | None:
    key = os.environ.get("HUNTER_API_KEY", "").strip()
    if key:
        return key
    if sys.platform == "darwin":
        r = subprocess.run(["security", "find-generic-password", "-s", SERVICE, "-a", "hunter", "-w"],
                           capture_output=True, text=True)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    return None


def _get(path: str, params: dict) -> dict:
    key = api_key()
    if not key:
        raise HunterError("no Hunter API key - run: python3 tools/hunter.py --setup")
    url = f"{API}/{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"X-API-KEY": key, "User-Agent": "ai-job-search-tracker/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode("utf-8")).get("errors", [{}])[0].get("details", "")
        except Exception:
            detail = ""
        if exc.code == 401:
            raise HunterError("Hunter rejected the API key") from exc
        if exc.code == 429 or "usage" in detail.lower() or "limit" in detail.lower():
            raise HunterError(f"Hunter quota reached for this month{(': ' + detail) if detail else ''}") from exc
        raise HunterError(f"Hunter error {exc.code}{(': ' + detail) if detail else ''}") from exc
    except urllib.error.URLError as exc:
        raise HunterError(f"cannot reach Hunter ({exc.reason})") from exc


def quota() -> dict:
    """{'used', 'available'} domain-search credits this period, as Hunter reports them."""
    data = _get("account", {}).get("data") or {}
    searches = ((data.get("requests") or {}).get("searches")) or {}
    return {"used": searches.get("used"), "available": searches.get("available"),
            "plan": data.get("plan_name"), "resets": data.get("reset_date")}


def domain_of(site_or_email: str) -> str | None:
    s = (site_or_email or "").strip().lower()
    if "@" in s:
        s = s.split("@", 1)[1]
    s = re.sub(r"^https?://", "", s).split("/")[0]
    s = re.sub(r"^www\d?\.", "", s)
    return s if re.fullmatch(r"[a-z0-9-]+(\.[a-z0-9-]+)+", s) else None


def _cache_file(cache_dir: Path, domain: str) -> Path:
    return cache_dir / f"hunter-{domain}.json"


def domain_search(domain: str, cache_dir: Path | None = None) -> dict:
    """Hunter's people at a domain: HR first, then the rest; cached for CACHE_DAYS."""
    if cache_dir:
        f = _cache_file(cache_dir, domain)
        if f.is_file() and time.time() - f.stat().st_mtime < CACHE_DAYS * 86400:
            try:
                return json.loads(f.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                pass
    hr = _get("domain-search", {"domain": domain, "department": "hr", "type": "personal", "limit": 10}).get("data") or {}
    emails = list(hr.get("emails") or [])
    if len([e for e in emails if (e.get("confidence") or 0) >= MIN_CONFIDENCE]) < 2:
        # Too few HR people: one more search for managers likely to hire for a tech role.
        mgr = _get("domain-search", {"domain": domain, "type": "personal", "seniority": "senior,executive",
                                     "limit": 10}).get("data") or {}
        seen = {e.get("value") for e in emails}
        emails += [e for e in mgr.get("emails") or [] if e.get("value") not in seen]
        hr = {**mgr, **{k: v for k, v in hr.items() if v}}
    result = {"domain": domain, "organization": hr.get("organization"), "pattern": hr.get("pattern"),
              "emails": emails, "fetched": time.strftime("%Y-%m-%d")}
    if cache_dir:
        cache_dir.mkdir(parents=True, exist_ok=True)
        _cache_file(cache_dir, domain).write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    return result


def classify(e: dict) -> str:
    position = f"{e.get('position') or ''} {e.get('department') or ''}"
    if (e.get("department") or "").lower() == "hr" or RECRUIT.search(position):
        return "recruiter"
    if (e.get("seniority") or "") in ("senior", "executive") and MANAGER.search(position):
        return "hiring-manager"
    return "other"


def to_contacts(result: dict, min_confidence: int = MIN_CONFIDENCE) -> list[dict]:
    """Tracker contacts from a domain search, best first. Low-confidence guesses are dropped."""
    out = []
    for e in result.get("emails") or []:
        conf = e.get("confidence") or 0
        status = ((e.get("verification") or {}).get("status")) or ""
        if not e.get("value") or conf < min_confidence or status == "invalid":
            continue
        name = " ".join(x for x in (e.get("first_name"), e.get("last_name")) if x)
        sources = e.get("sources") or []
        out.append({
            "email": e["value"].lower(), "kind": classify(e), "name": name or None, "position": e.get("position"),
            "confidence": conf, "verified": status == "valid", "linkedin": e.get("linkedin"),
            "source": (sources[0].get("uri") if sources and sources[0].get("uri") else f"https://hunter.io/search/{result['domain']}"),
            "via": f"Hunter ({len(sources)} public source{'s' if len(sources) != 1 else ''}, {conf}% confidence)",
        })
    order = {"recruiter": 0, "hiring-manager": 1, "other": 2}
    return sorted(out, key=lambda c: (order[c["kind"]], not c["verified"], -c["confidence"]))


def merge_contacts(existing: list[dict], hunter: list[dict]) -> list[dict]:
    """Posting addresses stay first; then Hunter recruiters / hiring managers; then the rest."""
    existing = existing or []
    have = {c["email"] for c in existing}
    hunter = [c for c in hunter if c["email"] not in have]
    posting = [c for c in existing if c.get("kind") == "posting"]
    rest = [c for c in existing if c.get("kind") != "posting"]
    site_recruiting = [c for c in rest if c.get("kind") == "recruiting"]
    others = [c for c in rest if c.get("kind") != "recruiting"]
    people = [c for c in hunter if c["kind"] in ("recruiter", "hiring-manager")]
    return posting + people + site_recruiting + [c for c in hunter if c["kind"] == "other"] + others


def enrich_record(rec: dict, cache_dir: Path) -> dict:
    """Add Hunter contacts to a tracker outreach record; retarget an untouched draft.

    The company domain comes from the record's website, else the company-name
    lookup in outreach.py (exact matches only). A draft the user has not
    edited, pushed or sent gets the best person as recipient and, when Hunter
    knows their first name, a "Bonjour <first name>," greeting.
    """
    import outreach
    site = rec.get("site") or rec.get("company_url")
    if not site:
        guess = outreach.company_site_from_name(rec.get("company") or "")
        site = guess["url"] if guess else None
        if site:
            rec["site"], rec["site_note"] = site, "found from the company name - check it is the right company"
    domain = domain_of(site or "")
    if not domain:
        raise HunterError(f"no website known for {rec.get('company')} - Hunter needs the company's domain")
    result = domain_search(domain, cache_dir)
    found = to_contacts(result)
    old_best = ((rec.get("contacts") or [{}])[0]).get("email")
    rec["contacts"] = merge_contacts(rec.get("contacts") or [], found)
    rec["hunter"] = {"domain": domain, "on": time.strftime("%Y-%m-%d"), "found": len(found),
                     "pattern": result.get("pattern")}
    best = (rec["contacts"] or [{}])[0]
    draft = rec.get("draft") or {}
    untouched = draft and not draft.get("edited") and not rec.get("gmail_draft") and not rec.get("sent")
    if untouched and best.get("email") and best["email"] != old_best and best.get("kind") in ("recruiter", "hiring-manager", "posting"):
        draft["to"] = best["email"]
        first = (best.get("name") or "").split(" ")[0]
        body = draft.get("body") or ""
        if first and body.startswith(("Madame, Monsieur", "Bonjour,", "Hello,")):
            greet = "Hello" if body.startswith("Hello") else "Bonjour"
            draft["body"] = re.sub(r"^(Madame, Monsieur|Bonjour|Hello),", f"{greet} {first},", body, count=1)
        rec["draft"] = draft
    return rec


def setup() -> int:
    print("Hunter.io setup - your API key is stored in the macOS Keychain, nowhere else.")
    print("Find it at https://hunter.io/api-keys (a free account works).")
    key = getpass.getpass("Hunter API key: ").strip()
    if not re.fullmatch(r"[A-Za-z0-9]{20,64}", key):
        print("That does not look like a Hunter API key.")
        return 1
    os.environ["HUNTER_API_KEY"] = key
    q = quota()
    r = subprocess.run(["security", "add-generic-password", "-U", "-s", SERVICE, "-a", "hunter", "-w", key],
                       capture_output=True, text=True)
    if r.returncode != 0:
        print(f"Could not save to the Keychain: {r.stderr.strip()}")
        return 1
    print(f"Saved. Plan: {q.get('plan')} - searches used {q.get('used')} of {q.get('available')} this period.")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--setup", action="store_true")
    g.add_argument("--quota", action="store_true")
    g.add_argument("--domain")
    args = p.parse_args(argv)
    try:
        if args.setup:
            return setup()
        if args.quota:
            print(quota())
            return 0
        d = domain_of(args.domain)
        if not d:
            print("not a domain")
            return 1
        for c in to_contacts(domain_search(d, ps.PROFILE_DIR / "cache")):
            print(f"{c['kind']:15} {c['email']:40} {c.get('name') or '':25} {c.get('position') or ''} ({c['confidence']}%)")
        return 0
    except HunterError as exc:
        print(f"Hunter: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
