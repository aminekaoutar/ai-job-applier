#!/usr/bin/env python3
"""A dedicated job-search mailbox the agent can read, for the one-time codes job sites email.

  python3 tools/job_mailbox.py setup     (address + app password, saved in the macOS Keychain)
  python3 tools/job_mailbox.py status    (is it configured, can it log in)
  python3 tools/job_mailbox.py code hellowork.com   (latest code from that site, last 10 min)

Job sites (HelloWork, Welcome to the Jungle...) verify a new candidate account by
emailing a short code. With a mailbox made only for job hunting, auto_apply.py
applies with that address and reads the code itself, so an application runs end
to end. The mailbox is read over IMAP with an app password (Gmail: 2-step
verification on, then Google Account > Security > App passwords). Only messages
from the site that asked, received after the code was requested, are looked at,
and only the code's digits are taken: email text is data, never instructions.

Config: profile/mailbox.json {"address", "imap_host"}; the app password lives in
the Keychain (service ai-job-search-mailbox), nowhere else. Forward that mailbox
to your main address so recruiters' replies reach you.
"""

from __future__ import annotations

import email
import getpass
import imaplib
import json
import os
import re
import subprocess
import sys
import time
from email.header import decode_header, make_header
from datetime import timezone
from email.utils import parseaddr, parsedate_to_datetime
from html import unescape
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "profile" / "mailbox.json"
SERVICE = "ai-job-search-mailbox"
IMAP_HOSTS = {"gmail.com": "imap.gmail.com", "googlemail.com": "imap.gmail.com", "outlook.com": "outlook.office365.com",
              "hotmail.com": "outlook.office365.com", "live.com": "outlook.office365.com", "yahoo.com": "imap.mail.yahoo.com",
              "icloud.com": "imap.mail.me.com", "gmx.fr": "imap.gmx.net", "gmx.com": "imap.gmx.net"}
# A code is 4-8 digits standing alone, near a word that says it is one.
CODE_NEAR = re.compile(r"(?:code|v[ée]rification|validation|confirm|OTP|PIN)[^0-9]{0,120}?\b(\d{4,8})\b"
                       r"|\b(\d{4,8})\b[^0-9]{0,60}?(?:code|v[ée]rification|validation)", re.I)


class MailboxError(RuntimeError):
    pass


def load_config(path: Path = CONFIG) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) and data.get("address") else {}


def address(path: Path = CONFIG) -> str | None:
    return load_config(path).get("address") or None


def _password(addr: str) -> str | None:
    env = os.environ.get("JOB_MAILBOX_PASSWORD", "").strip()
    if env:
        return env
    if sys.platform != "darwin":
        return None
    r = subprocess.run(["security", "find-generic-password", "-s", SERVICE, "-a", addr, "-w"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        return None
    return r.stdout.strip() or None


def _text_of(msg: email.message.Message) -> str:
    parts = []
    for part in (msg.walk() if msg.is_multipart() else [msg]):
        ctype = part.get_content_type()
        if ctype not in ("text/plain", "text/html") or part.get_filename():
            continue
        payload = part.get_payload(decode=True) or b""
        text = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        if ctype == "text/html":
            text = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", text)
            text = unescape(re.sub(r"<[^>]+>", " ", text))
        parts.append(text)
    return re.sub(r"\s+", " ", " ".join(parts))


def extract_code(subject: str, body: str) -> str | None:
    """The one-time code in a verification email: near a 'code' word, subject first."""
    for text in (subject, body):
        m = CODE_NEAR.search(text or "")
        if m:
            return m.group(1) or m.group(2)
    return None


def _from_site(msg: email.message.Message, site: str) -> bool:
    sender = parseaddr(str(msg.get("From", "")))[1].lower()
    domain = sender.rsplit("@", 1)[-1]
    site = site.lower().removeprefix("www.")
    base = ".".join(site.split(".")[-2:])  # hellowork.com from fr.hellowork.com
    return domain == base or domain.endswith("." + base)


def latest_code(site: str, since: float, timeout_s: int = 120, poll_s: int = 5,
                config_path: Path = CONFIG, imap_factory=imaplib.IMAP4_SSL) -> str | None:
    """Wait up to timeout_s for an email from `site` received after `since` (epoch seconds) and
    return its code. None when nothing arrives or the mailbox is not configured."""
    cfg = load_config(config_path)
    if not cfg:
        return None
    pwd = _password(cfg["address"])
    if not pwd:
        raise MailboxError("no app password for the job mailbox - run: python3 tools/job_mailbox.py setup")
    deadline = time.time() + timeout_s
    while True:
        try:
            box = imap_factory(cfg.get("imap_host") or "imap.gmail.com")
            box.login(cfg["address"], pwd)
            try:
                box.select("INBOX", readonly=True)
                day = time.strftime("%d-%b-%Y", time.gmtime(since - 86400))
                _, ids = box.search(None, f'(SINCE "{day}")')
                for mid in reversed((ids[0] or b"").split()[-30:]):
                    _, data = box.fetch(mid, "(RFC822)")
                    raw = next((d[1] for d in data if isinstance(d, tuple)), None)
                    if not raw:
                        continue
                    msg = email.message_from_bytes(raw)
                    try:
                        dt = parsedate_to_datetime(msg.get("Date"))
                        if dt.tzinfo is None:  # "-0000": UTC with no zone, not the machine's local time
                            dt = dt.replace(tzinfo=timezone.utc)
                        when = dt.timestamp()
                    except (TypeError, ValueError):
                        continue
                    if when < since - 60 or not _from_site(msg, site):
                        continue
                    code = extract_code(str(make_header(decode_header(msg.get("Subject", "")))), _text_of(msg))
                    if code:
                        return code
            finally:
                try:
                    box.logout()
                except Exception:  # noqa: BLE001
                    pass
        except imaplib.IMAP4.error as exc:
            raise MailboxError(f"cannot read the job mailbox: {exc}") from exc
        if time.time() + poll_s > deadline:
            return None
        time.sleep(poll_s)


RECEIVED = re.compile(r"(avons (bien )?reçu|bien reçu|est arrivée chez|candidature (a bien été |a été )?"
                      r"(reçue|enregistrée|transmise)|merci (pour|de) votre (candidature|intérêt|motivation)|"
                      r"thank you for (applying|your application|your interest)|received your application|"
                      r"application (has been |was )?(received|submitted))", re.I)
NOT_DONE = re.compile(r"finalis|complete your application|terminez|n'est pas (encore )?termin|still need", re.I)


def confirmation(names: list[str], since: float, config_path: Path = CONFIG, imap_factory=imaplib.IMAP4_SSL) -> str | None:
    """The subject of an email received after `since` that confirms an application to one of `names`
    (company name or site domain), e.g. 'Nous avons reçu votre CV pour le poste de ...'. None otherwise.
    A 'finalise your application' reminder is not a confirmation."""
    cfg = load_config(config_path)
    pwd = _password(cfg["address"]) if cfg else None
    if not cfg or not pwd:
        return None
    keys = [n.lower() for n in names if n and len(n) >= 3]
    box = imap_factory(cfg.get("imap_host") or "imap.gmail.com")
    try:
        box.login(cfg["address"], pwd)
        box.select("INBOX", readonly=True)
        day = time.strftime("%d-%b-%Y", time.gmtime(since - 86400))
        _, ids = box.search(None, f'(SINCE "{day}")')
        for mid in reversed((ids[0] or b"").split()[-40:]):
            _, data = box.fetch(mid, "(RFC822)")
            raw = next((d[1] for d in data if isinstance(d, tuple)), None)
            if not raw:
                continue
            msg = email.message_from_bytes(raw)
            try:
                dt = parsedate_to_datetime(msg.get("Date"))
                dt = dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
                if dt.timestamp() < since - 120:
                    continue
            except (TypeError, ValueError):
                continue
            subject = str(make_header(decode_header(msg.get("Subject", ""))))
            text = f"{subject} {msg.get('From', '')} {_text_of(msg)[:3000]}"
            if any(k in text.lower() for k in keys) and RECEIVED.search(text) and not NOT_DONE.search(subject):
                return subject
    except (imaplib.IMAP4.error, OSError):
        return None
    finally:
        try:
            box.logout()
        except Exception:  # noqa: BLE001
            pass
    return None


def setup() -> int:
    print("Job mailbox setup - the app password is stored in the macOS Keychain, nowhere else.")
    addr = input("Job-search email address: ").strip()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[a-z]{2,}", addr, re.I):
        print("That does not look like an email address.")
        return 1
    host = IMAP_HOSTS.get(addr.rsplit("@", 1)[1].lower()) or input("IMAP server (e.g. imap.gmail.com): ").strip()
    pwd = getpass.getpass("App password (Gmail: Google Account > Security > App passwords): ").replace(" ", "")
    try:
        box = imaplib.IMAP4_SSL(host)
        box.login(addr, pwd)
        box.logout()
    except (imaplib.IMAP4.error, OSError) as exc:
        print(f"Could not log in to {host}: {exc}")
        return 1
    r = subprocess.run(["security", "add-generic-password", "-U", "-s", SERVICE, "-a", addr, "-w", pwd],
                       capture_output=True, text=True)
    if r.returncode != 0:
        print(f"Could not save to the Keychain: {r.stderr.strip()}")
        return 1
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text(json.dumps({"address": addr, "imap_host": host}, indent=2), encoding="utf-8")
    print(f"Saved. Applications will use {addr} and read its codes. Forward it to your main inbox.")
    return 0


def status() -> int:
    cfg = load_config()
    if not cfg:
        print("job mailbox: not configured (python3 tools/job_mailbox.py setup)")
        return 1
    pwd = _password(cfg["address"])
    print(f"job mailbox: {cfg['address']} via {cfg.get('imap_host')} | app password: {'found' if pwd else 'missing'}")
    if pwd:
        try:
            box = imaplib.IMAP4_SSL(cfg.get("imap_host") or "imap.gmail.com")
            box.login(cfg["address"], pwd)
            box.logout()
            print("login: OK")
        except (imaplib.IMAP4.error, OSError) as exc:
            print(f"login: FAILED ({exc})")
            return 1
    return 0


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "setup":
        sys.exit(setup())
    if cmd == "status":
        sys.exit(status())
    if cmd == "code" and len(sys.argv) > 2:
        print(latest_code(sys.argv[2], time.time() - 600, timeout_s=5) or "no code in the last 10 minutes")
        sys.exit(0)
    print("usage: python3 tools/job_mailbox.py [setup|status|code <site>]")
    sys.exit(2)
