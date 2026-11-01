#!/usr/bin/env python3
"""Put prepared application emails into your Gmail Drafts folder - never sends.

Each prepared job that has a recruiter address and a drafted email becomes a
Gmail draft: To, subject and body from the tracker, the tailored CV PDF
attached. You open Gmail, read it, and click Send yourself.

Access is IMAP with a Google *app password* (needs 2-Step Verification:
https://myaccount.google.com/apppasswords). The only IMAP command that
writes is APPEND to the folder flagged \\Drafts. The password lives in the
macOS Keychain (service "ai-job-search-gmail"), never in the repo; the
address is kept in profile/daily/gmail.json.

  python3 tools/gmail_drafts.py --setup        once, in your own Terminal (asks for the app password)
  python3 tools/gmail_drafts.py --push [--max 30]   add drafts for prepared jobs not pushed yet
  python3 tools/gmail_drafts.py --status
"""

import argparse
import getpass
import imaplib
import json
import re
import subprocess
import sys
import time
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import batch  # noqa: E402
import profile_store as ps  # noqa: E402

SERVICE = "ai-job-search-gmail"
HOST = "imap.gmail.com"
DEFAULT_MAX = 30


class GmailError(RuntimeError):
    pass


def config_path(base: Path) -> Path:
    return base / "daily" / "gmail.json"


def load_config(base: Path) -> dict | None:
    p = config_path(base)
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else None
    except (OSError, json.JSONDecodeError):
        return None


def keychain_get(account: str) -> str | None:
    if sys.platform != "darwin":
        return None
    r = subprocess.run(["security", "find-generic-password", "-a", account, "-s", SERVICE, "-w"],
                       capture_output=True, text=True)
    return r.stdout.strip() or None if r.returncode == 0 else None


def keychain_set(account: str, password: str) -> None:
    r = subprocess.run(["security", "add-generic-password", "-U", "-a", account, "-s", SERVICE, "-w", password],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise GmailError(f"could not save to the Keychain: {r.stderr.strip()}")


def connect(address: str, password: str) -> imaplib.IMAP4_SSL:
    try:
        imap = imaplib.IMAP4_SSL(HOST, timeout=30)
        imap.login(address, password)
        return imap
    except imaplib.IMAP4.error as exc:
        raise GmailError("Gmail refused the login - check the address and the app password "
                         "(a normal password does not work)") from exc
    except OSError as exc:
        raise GmailError(f"cannot reach Gmail ({exc})") from exc


def drafts_folder(imap: imaplib.IMAP4_SSL) -> str:
    """The folder carrying the \\Drafts flag, whatever its display language."""
    typ, rows = imap.list()
    if typ == "OK":
        for raw in rows or []:
            line = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
            if "\\Drafts" in line:
                m = re.search(r'"([^"]+)"\s*$', line) or re.search(r"(\S+)\s*$", line)
                if m:
                    return m.group(1)
    return "[Gmail]/Drafts"


def remove_draft(imap: imaplib.IMAP4_SSL, folder: str, message_id: str) -> int:
    """Delete our own earlier draft - only the message with this exact Message-ID."""
    if not message_id or not re.fullmatch(r"<[^<>\s\"]+>", message_id):
        return 0
    imap.select(f'"{folder}"')
    typ, data = imap.uid("SEARCH", None, f'HEADER Message-ID "{message_id}"')
    uids = (data[0] or b"").split() if typ == "OK" and data else []
    for uid in uids:
        imap.uid("STORE", uid, "+FLAGS", r"(\Deleted)")
        imap.uid("EXPUNGE", uid)  # UIDPLUS: expunges this message only
    return len(uids)


def recipient(rec: dict) -> str | None:
    """The draft's own "To", else the best address that is not a guess."""
    to = ((rec.get("draft") or {}).get("to") or "").strip()
    if to:
        return to
    return next((c["email"] for c in rec.get("contacts") or [] if c.get("kind") != "guess"), None)


def build_message(sender: str, sender_name: str, rec: dict, base: Path) -> EmailMessage:
    draft = rec["draft"]
    to = recipient(rec)
    msg = EmailMessage()
    msg["From"] = formataddr((sender_name, sender)) if sender_name else sender
    msg["To"] = to
    msg["Subject"] = draft.get("subject") or f"Candidature – {rec.get('title', '')}"
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=sender.split("@")[-1])
    msg.set_content(draft.get("body") or "")
    pdf = base / rec["pdf"] if rec.get("pdf") else None
    if pdf and pdf.is_file():
        name = re.sub(r"[^A-Za-z0-9_-]+", "_", f"CV_{sender_name or 'candidat'}_{rec.get('company', '')}").strip("_")
        msg.add_attachment(pdf.read_bytes(), maintype="application", subtype="pdf", filename=f"{name}.pdf")
    return msg


def pending(base: Path) -> list[dict]:
    """Prepared jobs with an address and a draft, not pushed and not sent - best match first."""
    rows = [r for r in batch.Store(base).all().values()
            if recipient(r) and (r.get("draft") or {}).get("body") and not r.get("gmail_draft") and not r.get("sent")]
    return sorted(rows, key=lambda r: -(r.get("match") or 0))


def push(base: Path = ps.PROFILE_DIR, max_drafts: int = DEFAULT_MAX, keys: list[str] | None = None) -> dict:
    cfg = load_config(base)
    if not cfg or not cfg.get("address"):
        raise GmailError("Gmail is not set up - run: python3 tools/gmail_drafts.py --setup")
    password = keychain_get(cfg["address"])
    if not password:
        raise GmailError("no app password in the Keychain - run: python3 tools/gmail_drafts.py --setup")
    rows = pending(base)
    if keys is not None:
        rows = [r for r in batch.Store(base).all().values() if r.get("key") in set(keys)
                and recipient(r) and (r.get("draft") or {}).get("body")]
    rows = rows[:max_drafts]
    if not rows:
        return {"pushed": 0, "folder": None, "left": len(pending(base))}
    name = ps.load_profile(base)["basics"].get("name") or ""
    store = batch.Store(base)
    imap = connect(cfg["address"], password)
    try:
        folder = drafts_folder(imap)
        pushed = 0
        for rec in rows:
            old = (rec.get("gmail_draft") or {}).get("message_id")
            if old and keys is not None:  # an explicit update replaces the earlier draft
                remove_draft(imap, folder, old)
            msg = build_message(cfg["address"], name, rec, base)
            typ, _ = imap.append(f'"{folder}"', r"(\Draft \Seen)", imaplib.Time2Internaldate(time.time()), msg.as_bytes())
            if typ != "OK":
                continue
            rec = store.load(rec["key"])
            rec["gmail_draft"] = {"on": time.strftime("%Y-%m-%d"), "to": msg["To"], "message_id": msg["Message-ID"]}
            store.save(rec["key"], rec)
            pushed += 1
    finally:
        try:
            imap.logout()
        except Exception:
            pass
    return {"pushed": pushed, "folder": folder, "left": len(pending(base))}


def setup(base: Path) -> int:
    print("Gmail drafts setup - the app password is stored in your macOS Keychain, nowhere else.")
    print("Create one at https://myaccount.google.com/apppasswords (needs 2-Step Verification).")
    address = input("Gmail address: ").strip()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[a-z]{2,}", address, re.I):
        print("That does not look like an email address.")
        return 1
    password = getpass.getpass("App password (16 letters, spaces are fine): ").replace(" ", "")
    print("Checking with Gmail…")
    imap = connect(address, password)
    folder = drafts_folder(imap)
    imap.logout()
    keychain_set(address, password)
    config_path(base).parent.mkdir(parents=True, exist_ok=True)
    config_path(base).write_text(json.dumps({"address": address, "max_per_day": DEFAULT_MAX}, indent=2), encoding="utf-8")
    print(f"Done. Drafts will go to '{folder}'. Nothing is ever sent for you.")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--setup", action="store_true")
    g.add_argument("--push", action="store_true")
    g.add_argument("--status", action="store_true")
    p.add_argument("--max", type=int, default=DEFAULT_MAX)
    args = p.parse_args(argv)
    base = ps.PROFILE_DIR
    try:
        if args.setup:
            return setup(base)
        if args.status:
            cfg = load_config(base)
            print(f"Gmail: {cfg['address'] if cfg else 'not set up'} · {len(pending(base))} drafts waiting to be pushed")
            return 0
        print(push(base, args.max))
        return 0
    except GmailError as exc:
        print(f"Gmail: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
