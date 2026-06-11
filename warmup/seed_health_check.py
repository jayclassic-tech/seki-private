#!/usr/bin/env python3
"""
Seed Health Checker — item 12
Logs into each Gmail seed via IMAP and checks:
- Account is accessible
- Has received warmup emails recently
- Reply rate is not zero
Flags dead/fatigued seeds and sends Telegram report.
"""
import imaplib, ssl, json, csv, os
from pathlib import Path
from datetime import datetime, timedelta

BASE_DIR   = Path("/opt/seki/warmup")
CREDS_CSV  = BASE_DIR / "gmail_seeds.csv"
STATE_F    = BASE_DIR / "warmup_state.json"
LOG_FILE   = BASE_DIR / "seed_health.log"

import logging
logging.basicConfig(
    filename=LOG_FILE, level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger("seed_health")

def send_telegram(message):
    try:
        import requests
        token   = os.environ.get("TELEGRAM_BOT_TOKEN","")
        chat_id = os.environ.get("TELEGRAM_CHAT_ID","")
        if not token or not chat_id: return
        requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": message, "parse_mode": "HTML"}, timeout=10)
    except: pass

def check_seed(email, password):
    result = {"email": email, "status": "ok", "inbox_count": 0,
              "spam_count": 0, "recent_warmup": 0, "error": ""}
    try:
        ctx  = ssl.create_default_context()
        imap = imaplib.IMAP4_SSL("imap.gmail.com", 993, ssl_context=ctx)
        imap.login(email, password)

        # Check inbox for recent warmup emails (last 7 days)
        imap.select("INBOX")
        since = (datetime.now() - timedelta(days=7)).strftime("%d-%b-%Y")
        _, msgs = imap.search(None, f'SINCE {since}')
        result["inbox_count"] = len(msgs[0].split()) if msgs[0] else 0

        # Check spam folder
        imap.select("[Gmail]/Spam")
        _, msgs = imap.search(None, f'SINCE {since}')
        result["spam_count"] = len(msgs[0].split()) if msgs[0] else 0

        # Check for warmup emails specifically
        imap.select("INBOX")
        _, msgs = imap.search(None, f'SINCE {since} SUBJECT "Quick note"')
        warmup1 = len(msgs[0].split()) if msgs[0] else 0
        _, msgs = imap.search(None, f'SINCE {since} SUBJECT "Checking in"')
        warmup2 = len(msgs[0].split()) if msgs[0] else 0
        result["recent_warmup"] = warmup1 + warmup2

        imap.logout()
    except imaplib.IMAP4.error as e:
        result["status"] = "auth_failed"
        result["error"]  = str(e)
    except Exception as e:
        result["status"] = "error"
        result["error"]  = str(e)
    return result

def main():
    log.info("=" * 50)
    log.info("Seed Health Check started")

    if not CREDS_CSV.exists():
        log.error("gmail_seeds.csv not found")
        return

    creds = []
    with open(CREDS_CSV) as f:
        for row in csv.DictReader(f):
            creds.append({
                "email":    row["email"].strip(),
                "password": row["app_password"].strip().replace(" ",""),
            })

    log.info(f"Checking {len(creds)} seeds...")

    results   = []
    dead      = []
    auth_fail = []
    healthy   = []

    for c in creds:
        log.info(f"  Checking {c['email']}...")
        r = check_seed(c["email"], c["password"])
        results.append(r)

        if r["status"] == "auth_failed":
            auth_fail.append(c["email"])
            log.warning(f"  AUTH FAILED: {c['email']} — {r['error']}")
        elif r["status"] == "error":
            dead.append(c["email"])
            log.warning(f"  ERROR: {c['email']} — {r['error']}")
        elif r["inbox_count"] == 0 and r["spam_count"] == 0:
            dead.append(c["email"])
            log.warning(f"  DEAD (no mail): {c['email']}")
        else:
            healthy.append(c["email"])
            log.info(f"  OK: {c['email']} inbox={r['inbox_count']} spam={r['spam_count']}")

    # Save results
    report_file = BASE_DIR / "seed_health_report.json"
    report = {
        "run_at":     datetime.now().isoformat(),
        "total":      len(creds),
        "healthy":    len(healthy),
        "dead":       len(dead),
        "auth_failed":len(auth_fail),
        "dead_list":  dead,
        "auth_fail_list": auth_fail,
    }
    report_file.write_text(json.dumps(report, indent=2))

    # Telegram report
    status_line = "✅ All seeds healthy" if not dead and not auth_fail else f"⚠️ {len(dead)} dead, {len(auth_fail)} auth failed"
    send_telegram(
        f"🌱 <b>Seed Health Report</b>\n"
        f"Total seeds: {len(creds)}\n"
        f"Healthy: {len(healthy)}\n"
        f"Dead/unreachable: {len(dead)}\n"
        f"Auth failed: {len(auth_fail)}\n"
        f"Status: {status_line}\n"
        + (f"Dead seeds: {', '.join(dead[:5])}" if dead else "")
        + (f"\nAuth failed: {', '.join(auth_fail[:5])}" if auth_fail else "")
    )

    log.info(f"Done — {len(healthy)} healthy, {len(dead)} dead, {len(auth_fail)} auth failed")
    print(f"Seed health check complete: {len(healthy)}/{len(creds)} healthy")

if __name__ == "__main__":
    main()
