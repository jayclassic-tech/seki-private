#!/usr/bin/env python3
"""
Reply Engine — Warmup Auto-Responder
Logs into each Gmail seed via IMAP, finds warmup emails,
marks as important, rescues from spam, sends reply.
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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler()]
)
log = logging.getLogger("reply_engine")

IMAP_HOST = "imap.gmail.com"
IMAP_PORT = 993
SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587

WARMUP_DOMAINS = ["supportcallsonline.com", "ldgauthenticator.com"]

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
]

def decode_str(s):
    if s is None:
        return ""
    decoded, enc = decode_header(s)[0]
    if isinstance(decoded, bytes):
        return decoded.decode(enc or "utf-8", errors="replace")
    return decoded

def is_warmup_email(from_addr):
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

def process_account(cred):
    addr     = cred["email"]
    password = cred["password"]
    stats    = {"replied": 0, "rescued_from_spam": 0, "errors": 0}

    try:
        imap = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
        imap.login(addr, password)
        log.info(f"[{addr}] IMAP connected")

        messages_to_reply = []

        for folder, is_spam in [("INBOX", False), ("[Gmail]/Spam", True)]:
            try:
                imap.select(folder)
            except Exception:
                continue

            _, data = imap.search(None, "UNSEEN")
            uids = data[0].split()
            if not uids:
                continue

            log.info(f"[{addr}] {folder}: {len(uids)} unread")

            for uid in uids:
                try:
                    _, msg_data = imap.fetch(uid, "(RFC822)")
                    if not msg_data or not msg_data[0] or not msg_data[0][1]:
                        continue
                    msg       = email.message_from_bytes(msg_data[0][1])
                    from_addr = decode_str(msg.get("From", ""))
                    subject   = decode_str(msg.get("Subject", ""))
                except Exception as fetch_err:
                    log.warning(f"[{addr}] Skipping unreadable email: {fetch_err}")
                    continue

                if not is_warmup_email(from_addr):
                    continue

                log.info(f"[{addr}] Found: '{subject}' from {from_addr}")

                if is_spam:
                    try:
                        imap.copy(uid, "INBOX")
                        imap.store(uid, "+FLAGS", "\\Deleted")
                        imap.expunge()
                        stats["rescued_from_spam"] += 1
                        log.info(f"[{addr}] Rescued from spam → inbox")
                    except Exception as rescue_err:
                        log.warning(f"[{addr}] Rescue failed: {rescue_err}")
                        continue

                imap.store(uid, "+FLAGS", "\\Seen \\Flagged")
                msg_id = msg.get("Message-ID")
                if not msg_id:
                    log.warning(f"[{addr}] Skipping email with no Message-ID")
                    continue
                messages_to_reply.append({
                    "to":      from_addr,
                    "subject": subject,
                    "msg_id":  msg_id,
                })
        imap.logout()

        if not messages_to_reply:
            log.info(f"[{addr}] No warmup emails found")
            return stats

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
            smtp.sendmail(addr, [item["to"]], reply.as_string())
            stats["replied"] += 1
            log.info(f"[{addr}] ✅ Replied to {item['to']}")

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
    log.info(f"Reply Engine — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
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
