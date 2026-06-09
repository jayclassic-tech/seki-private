#!/usr/bin/env python3
"""
Spam Rescue Engine — Standalone
Runs daily at 6pm via cron.
For each seed account:
  1. Find warmup emails in spam
  2. Move to inbox
  3. Mark seen + starred
  4. Reply to 35% of them (max 12 per account)
  5. Write stats to warmup_state.json

Cron: 0 18 * * * /opt/seki/warmup/venv/bin/python3 /opt/seki/warmup/spam_rescue.py
"""

import csv, imaplib, smtplib, email, random, time, json, logging
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.header import decode_header
from pathlib import Path
from datetime import datetime, date

BASE_DIR   = Path("/opt/seki/warmup")
CREDS_CSV  = BASE_DIR / "gmail_seeds.csv"
DOMAINS_F  = BASE_DIR / "warmup_domains.json"
STATE_FILE = BASE_DIR / "warmup_state.json"
LOG_FILE   = BASE_DIR / "spam_rescue.log"

IMAP_HOST  = "imap.gmail.com"
IMAP_PORT  = 993
SMTP_HOST  = "smtp.gmail.com"
SMTP_PORT  = 587

MAX_REPLIES_PER_ACCOUNT = 12
REPLY_RATE              = 0.35   # reply to 35% of rescued emails
REPLY_DELAY_MIN         = 10     # seconds between replies
REPLY_DELAY_MAX         = 22

# ── Logger ────────────────────────────────────────────────────
logger = logging.getLogger("spam_rescue")
logger.handlers.clear()
logger.propagate = False
logger.setLevel(logging.INFO)
fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
fh  = logging.FileHandler(LOG_FILE)
fh.setFormatter(fmt)
sh  = logging.StreamHandler()
sh.setFormatter(fmt)
logger.addHandler(fh)
logger.addHandler(sh)
log = logger

REPLY_VARIANTS = [
    "Thanks for reaching out! Got your message — appreciate the update.",
    "Thanks, noted. Appreciate you sending this over.",
    "Got it, thank you for the message!",
    "Thanks for the note. All good on my end.",
    "Received, thanks! Hope you have a great day.",
    "Thanks for this! Really appreciate the update.",
    "Got your message. Thanks for keeping me in the loop.",
    "Appreciate it, thanks for reaching out!",
    "Thanks — received and noted.",
    "Thank you! Good to hear from you.",
    "Received! Thanks for the heads up.",
    "Appreciated — thanks for the message.",
    "Got it! Thanks for the update.",
    "Thanks, will keep this in mind.",
    "Received and noted, thanks!",
]

# ── Helpers ───────────────────────────────────────────────────
def decode_str(s):
    if not s: return ""
    decoded, enc = decode_header(s)[0]
    if isinstance(decoded, bytes):
        return decoded.decode(enc or "utf-8", errors="replace")
    return decoded

def load_domains():
    if DOMAINS_F.exists():
        with open(DOMAINS_F) as f:
            return json.load(f)
    return []

def load_credentials():
    creds = []
    with open(CREDS_CSV) as f:
        for row in csv.DictReader(f):
            creds.append({
                "email":    row["email"].strip(),
                "password": row["app_password"].strip().replace(" ", ""),
            })
    return creds

def load_state():
    if STATE_FILE.exists():
        with open(STATE_FILE) as f:
            return json.load(f)
    return {}

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

def is_warmup_email(from_addr, domains):
    if any(x in from_addr.lower() for x in [
        "mailer-daemon", "postmaster", "noreply@accounts.google",
        "no-reply@", "notifications@"
    ]):
        return False
    return any(domain in from_addr.lower() for domain in domains)

def send_telegram(message: str):
    """Send Telegram alert if credentials are configured."""
    try:
        import os, requests
        token   = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
        if not token or not chat_id:
            return
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": message, "parse_mode": "HTML"},
            timeout=10
        )
    except Exception:
        pass

# ── Per-account processing ────────────────────────────────────
def process_account(cred, domains):
    addr     = cred["email"]
    password = cred["password"]
    rescued  = []
    stats    = {"rescued": 0, "replied": 0, "errors": 0}

    try:
        imap = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
        imap.login(addr, password)
    except Exception as e:
        log.error(f"[{addr}] Login failed: {e}")
        stats["errors"] += 1
        return stats

    log.info(f"[{addr}] Connected")

    try:
        # ── Search spam ──────────────────────────────────────
        imap.select("[Gmail]/Spam")
        _, data = imap.uid("SEARCH", None, "ALL")
        uids = data[0].split() if data[0] else []

        if not uids:
            log.info(f"[{addr}] Spam is empty")
            imap.logout()
            return stats

        log.info(f"[{addr}] {len(uids)} emails in spam — scanning...")

        for uid in uids:
            try:
                _, msg_data = imap.uid("FETCH", uid, "(RFC822)")
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
                log.warning(f"[{addr}] Unreadable email: {e}")
                continue

            if not is_warmup_email(from_addr, domains):
                continue

            if not msg_id:
                log.warning(f"[{addr}] No Message-ID from {from_addr}, skipping")
                continue

            log.info(f"[{addr}] Rescuing: '{subject[:45]}' from {from_addr}")

            # ── Rescue: spam → inbox ─────────────────────────
            try:
                imap.uid("STORE", uid, "-X-GM-LABELS", "\\Spam")
                imap.uid("COPY",  uid, "INBOX")
                imap.uid("STORE", uid, "+FLAGS", "\\Deleted")
                imap.expunge()
            except Exception as e:
                log.warning(f"[{addr}] Rescue move failed: {e}")
                continue

            # ── Mark seen + starred in inbox ─────────────────
            try:
                imap.select("INBOX")
                _, d = imap.uid("SEARCH", None, f'HEADER Message-ID "{msg_id}"')
                inbox_uids = d[0].split() if d[0] else []
                for iuid in inbox_uids:
                    imap.uid("STORE", iuid, "+FLAGS", "\\Seen \\Flagged")
                imap.select("[Gmail]/Spam")
                log.info(f"[{addr}] ✅ Rescued → inbox, seen + starred")
                stats["rescued"] += 1
                rescued.append({"to": from_addr, "subject": subject, "msg_id": msg_id})
            except Exception as e:
                log.warning(f"[{addr}] Star/seen failed: {e}")

        imap.logout()

    except Exception as e:
        log.error(f"[{addr}] IMAP error: {e}")
        stats["errors"] += 1
        try: imap.logout()
        except: pass
        return stats

    # ── Send replies (35%, max 12) ────────────────────────────
    if not rescued:
        log.info(f"[{addr}] No warmup emails rescued")
        return stats

    # Randomly select 35% to reply to, capped at MAX_REPLIES_PER_ACCOUNT
    n_reply  = min(
        MAX_REPLIES_PER_ACCOUNT,
        max(1, int(len(rescued) * REPLY_RATE))
    )
    to_reply = random.sample(rescued, min(n_reply, len(rescued)))

    log.info(f"[{addr}] Replying to {len(to_reply)}/{len(rescued)} rescued emails")

    try:
        smtp = smtplib.SMTP(SMTP_HOST, SMTP_PORT)
        smtp.ehlo()
        smtp.starttls()
        smtp.login(addr, password)

        for item in to_reply:
            delay = random.uniform(REPLY_DELAY_MIN, REPLY_DELAY_MAX)
            time.sleep(delay)

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
                log.warning(f"[{addr}] Reply failed: {e}")
                stats["errors"] += 1

        smtp.quit()

    except smtplib.SMTPAuthenticationError as e:
        log.error(f"[{addr}] SMTP auth error: {e}")
        stats["errors"] += 1
    except Exception as e:
        log.error(f"[{addr}] SMTP error: {e}")
        stats["errors"] += 1

    return stats

# ── Main ──────────────────────────────────────────────────────
def main():
    log.info("=" * 60)
    log.info(f"Spam Rescue Engine — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    domains = load_domains()
    creds   = load_credentials()
    state   = load_state()

    log.info(f"Accounts: {len(creds)} | Watching domains: {', '.join(domains)}")

    total_rescued = 0
    total_replied = 0
    total_errors  = 0

    for i, cred in enumerate(creds):
        if i > 0:
            gap = random.uniform(8, 20)
            log.info(f"Waiting {gap:.0f}s before next account...")
            time.sleep(gap)

        stats = process_account(cred, domains)
        total_rescued += stats["rescued"]
        total_replied += stats["replied"]
        total_errors  += stats["errors"]

    # ── Write rescue counts to warmup_state.json ─────────────
    today = str(date.today())
    for profile_name, profile_data in state.items():
        if "rescue_log" not in profile_data:
            profile_data["rescue_log"] = {}
        # Add today's rescue count (distributed across profiles)
        existing = profile_data["rescue_log"].get(today, 0)
        profile_data["rescue_log"][today] = existing

    # Write total rescues to state under a global key
    if "_rescue_stats" not in state:
        state["_rescue_stats"] = {}
    state["_rescue_stats"][today] = {
        "rescued": total_rescued,
        "replied": total_replied,
        "errors":  total_errors,
        "run_at":  datetime.now().isoformat()
    }
    save_state(state)

    # ── Summary ───────────────────────────────────────────────
    log.info("─" * 60)
    log.info(f"Rescued: {total_rescued} | Replied: {total_replied} | Errors: {total_errors}")
    log.info("=" * 60)

    # ── Telegram summary ──────────────────────────────────────
    if total_rescued > 0:
        send_telegram(
            f"🛡 <b>Spam Rescue Complete</b>\n"
            f"Date: {today}\n"
            f"Rescued: <b>{total_rescued}</b> emails → inbox\n"
            f"Replied: <b>{total_replied}</b>\n"
            f"Errors: {total_errors}"
        )

if __name__ == "__main__":
    main()
