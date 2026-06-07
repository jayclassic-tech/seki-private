#!/usr/bin/env python3
"""
Reply Engine — Warmup Auto-Responder v2
Fixes: duplicate logging, NoneType on rescued emails, reply-after-rescue
"""

import csv, imaplib, smtplib, email, logging, random, time
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.header import decode_header
from pathlib import Path
from datetime import datetime

BASE_DIR  = Path("/opt/seki/warmup")
CREDS_CSV = BASE_DIR / "gmail_seeds.csv"
LOG_FILE  = BASE_DIR / "reply_engine.log"

# --- Fix duplicate logging: clear handlers before adding ---
logger = logging.getLogger("reply_engine")
if not logger.handlers:
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    fh = logging.FileHandler(LOG_FILE)
    fh.setFormatter(fmt)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    logger.addHandler(fh)
    logger.addHandler(sh)
log = logger

IMAP_HOST = "imap.gmail.com"
IMAP_PORT = 993
SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587

WARMUP_DOMAINS = ["supportcallsonline.com", "ldgauthenticator.com", "binancehelps.net"]

REPLY_VARIANTS = [
    "Thanks for reaching out! Got your message — appreciate the update.",
    "Thanks, noted. Appreciate you sending this over.",
    "Got it, thank you for the message!",
    "Thanks for the note. All good on my end.",
    "Received, thanks! Hope you have a great day too.",
    "Thanks for this! Really appreciate the update.",
    "Got your message. Thanks for keeping me in the loop.",
    "Appreciate it, thanks for reaching out!",
    "Thanks — received and noted.",
    "Thank you! Good to hear from you.",
    "Received! Thanks for the heads up.",
    "Appreciated — thanks for the message.",
]

def decode_str(s):
    if s is None:
        return ""
    decoded, enc = decode_header(s)[0]
    if isinstance(decoded, bytes):
        return decoded.decode(enc or "utf-8", errors="replace")
    return decoded

def is_warmup_email(from_addr):
    if any(x in from_addr.lower() for x in ["mailer-daemon", "postmaster", "noreply@accounts.google"]):
        return False
    return any(domain in from_addr.lower() for domain in WARMUP_DOMAINS)

def load_credentials():
    creds = []
    with open(CREDS_CSV) as f:
        for row in csv.DictReader(f):
            creds.append({
                "email":    row["email"].strip(),
                "password": row["app_password"].strip().replace(" ", ""),
            })
    return creds

def fetch_warmup_emails(imap, addr, folder, is_spam):
    """
    Search a folder for unseen warmup emails.
    If spam, rescue to INBOX first, then return metadata for reply.
    Returns list of dicts: {to, subject, msg_id}
    """
    results = []
    try:
        status, _ = imap.select(folder)
        if status != "OK":
            return results
    except Exception:
        return results

    _, data = imap.search(None, "UNSEEN")
    uids = data[0].split() if data[0] else []
    if not uids:
        return results

    log.info(f"[{addr}] {folder}: {len(uids)} unread")

    for uid in uids:
        try:
            _, msg_data = imap.fetch(uid, "(RFC822)")
            if not msg_data or not isinstance(msg_data[0], tuple):
                continue
            raw = msg_data[0][1]
            if not raw:
                continue
            msg       = email.message_from_bytes(raw)
            from_addr = decode_str(msg.get("From", ""))
            subject   = decode_str(msg.get("Subject", ""))
            msg_id    = msg.get("Message-ID", "").strip()
        except Exception as e:
            log.warning(f"[{addr}] Skipping unreadable email: {e}")
            continue

        if not is_warmup_email(from_addr):
            continue
        if not msg_id:
            log.warning(f"[{addr}] Skipping email with no Message-ID from {from_addr}")
            continue

        log.info(f"[{addr}] Found: '{subject}' from {from_addr}")

        if is_spam:
            # Rescue: copy to INBOX, delete from spam
            try:
                imap.copy(uid, "INBOX")
                imap.store(uid, "+FLAGS", "\\Deleted")
                imap.expunge()
                log.info(f"[{addr}] Rescued from spam → inbox")
            except Exception as e:
                log.warning(f"[{addr}] Rescue failed: {e}")
                continue
        else:
            # Mark as seen + starred in INBOX
            try:
                imap.store(uid, "+FLAGS", "\\Seen \\Flagged")
            except Exception:
                pass

        results.append({
            "to":      from_addr,
            "subject": subject,
            "msg_id":  msg_id,
        })

    return results

def process_account(cred):
    addr     = cred["email"]
    password = cred["password"]
    stats    = {"replied": 0, "rescued_from_spam": 0, "errors": 0}

    try:
        imap = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
        imap.login(addr, password)
        log.info(f"[{addr}] IMAP connected")

        messages_to_reply = []

        # Check INBOX first
        inbox_msgs = fetch_warmup_emails(imap, addr, "INBOX", is_spam=False)
        messages_to_reply.extend(inbox_msgs)

        # Check Spam — rescue + collect for reply
        spam_msgs = fetch_warmup_emails(imap, addr, "[Gmail]/Spam", is_spam=True)
        stats["rescued_from_spam"] += len(spam_msgs)
        messages_to_reply.extend(spam_msgs)

        imap.logout()

        if not messages_to_reply:
            log.info(f"[{addr}] No warmup emails found")
            return stats

        # Send replies via SMTP
        smtp = smtplib.SMTP(SMTP_HOST, SMTP_PORT)
        smtp.ehlo()
        smtp.starttls()
        smtp.login(addr, password)

        for item in messages_to_reply:
            time.sleep(random.uniform(3, 8))
            reply = MIMEMultipart("alternative")
            reply["From"]        = addr
            reply["To"]          = item["to"]
            reply["Subject"]     = f"Re: {item['subject']}"
            reply["In-Reply-To"] = item["msg_id"]
            reply["References"]  = item["msg_id"]
            reply.attach(MIMEText(random.choice(REPLY_VARIANTS), "plain"))
            try:
                smtp.sendmail(addr, [item["to"]], reply.as_string())
                stats["replied"] += 1
                log.info(f"[{addr}] ✅ Replied to {item['to']}")
            except Exception as e:
                log.warning(f"[{addr}] Reply failed to {item['to']}: {e}")
                stats["errors"] += 1

        smtp.quit()

    except imaplib.IMAP4.error as e:
        log.error(f"[{addr}] IMAP error: {e}")
        stats["errors"] += 1
    except smtplib.SMTPAuthenticationError as e:
        log.error(f"[{addr}] SMTP auth error: {e}")
        stats["errors"] += 1
    except Exception as e:
        log.error(f"[{addr}] Error: {e}")
        stats["errors"] += 1

    return stats

def main():
    log.info("=" * 60)
    log.info(f"Reply Engine v2 — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    creds = load_credentials()
    log.info(f"Loaded {len(creds)} accounts")

    total_replied = total_rescued = total_errors = 0

    for i, cred in enumerate(creds):
        if i > 0:
            time.sleep(random.uniform(5, 15))
        stats = process_account(cred)
        total_replied += stats["replied"]
        total_rescued += stats["rescued_from_spam"]
        total_errors  += stats["errors"]

    log.info("─" * 60)
    log.info(f"Replied: {total_replied} | Rescued: {total_rescued} | Errors: {total_errors}")
    log.info("=" * 60)

if __name__ == "__main__":
    main()
