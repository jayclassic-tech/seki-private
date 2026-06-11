#!/usr/bin/env python3
"""
Warmup Engine v2 — Seki Mailer
Features:
  - Per-profile log files (warmup_SEKI.log, warmup_LDGAUTH.log etc)
  - Health score calculation (0-100) written to warmup_state.json
  - Auto-pause if spam rate >20% for 2 consecutive days + Telegram alert
  - Graduation detection: day 21+ and health score >80 + Telegram alert
  - Skips paused or graduated profiles
  - Full state written after every run
"""

import os, csv, json, random, logging, subprocess, time
from datetime import datetime, date
from pathlib import Path

BASE_DIR       = Path("/opt/seki/warmup")
SEEDS_CSV      = BASE_DIR / "seeds.csv"
STATE_FILE     = BASE_DIR / "warmup_state.json"
TEMPLATES_DIR  = BASE_DIR / "templates"
BATCH_DIR      = BASE_DIR / "batches"
LOG_FILE       = BASE_DIR / "warmup.log"          # master log
MAILBOXES_FILE = BASE_DIR / "mailboxes.json"
PROFILES_FILE  = BASE_DIR / "warmup_profiles.json"
SEKI_BIN       = "/opt/seki/postfix_mailer.py"

BATCH_DIR.mkdir(exist_ok=True)

GRADUATION_DAY          = 21
GRADUATION_HEALTH       = 85    # raised from 80 — item 11
AUTO_PAUSE_SPAM_RATE    = 0.20   # 20%
AUTO_PAUSE_CONSECUTIVE  = 2      # days
GRADUATION_CONSECUTIVE  = 7      # days health must stay above threshold — item 11
RATE_LIMIT_THRESHOLD    = 3      # 421 hits before auto-pause — item 9

# ── Master logger ─────────────────────────────────────────────
def make_logger(name, log_path):
    lg = logging.getLogger(name)
    lg.handlers.clear()
    lg.propagate = False
    lg.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    fh = logging.FileHandler(log_path)
    fh.setFormatter(fmt)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    lg.addHandler(fh)
    lg.addHandler(sh)
    return lg

log = make_logger("warmup", LOG_FILE)

# ── Ramp schedule ─────────────────────────────────────────────
# (max_day, emails_per_mailbox, rate_min, rate_max)
RAMP = [
    (1,    4,  8.0, 12.0),
    (2,    6,  5.0,  8.0),
    (3,   10,  3.0,  6.0),
    (5,   15,  2.0,  4.0),
    (7,   20,  1.5,  3.0),
    (10,  30,  1.0,  2.0),
    (999, 50,  0.5,  1.5),
]

# Daily send limit ramp — maps day to recommended daily limit
DAILY_LIMIT_RAMP = [
    (3,    40),
    (5,   120),
    (7,   200),
    (10,  360),
    (999, 600),
]

def get_ramp(day):
    for max_day, volume, rmin, rmax in RAMP:
        if day <= max_day:
            return volume, rmin, rmax
    return RAMP[-1][1], RAMP[-1][2], RAMP[-1][3]

def get_recommended_limit(day):
    for max_day, limit in DAILY_LIMIT_RAMP:
        if day <= max_day:
            return limit
    return DAILY_LIMIT_RAMP[-1][1]

# ── Telegram ──────────────────────────────────────────────────
def get_base_domain(email_or_host):
    host  = email_or_host.split("@")[-1] if "@" in email_or_host else email_or_host
    parts = host.strip().split(".")
    return ".".join(parts[-2:]) if len(parts) > 2 else host

def check_gmail_rate_limit(profile_name, domains_list):
    """
    Check mail.log for 421-4.7.28 rate limit errors for this profile's domains.
    Returns the subdomain that triggered the limit, or None.
    """
    try:
        import subprocess
        log_tail = subprocess.run(
            ["tail", "-n", "500", "/var/log/mail.log"],
            capture_output=True, text=True
        ).stdout
        hits = {}
        for line in log_tail.splitlines():
            if "421-4.7.28" not in line: continue
            import re
            m = re.search(r'domain \[(\S+?)[\s\]]', line)
            if not m: continue
            subdomain = m.group(1).rstrip("]").rstrip()
            # Check if this subdomain belongs to one of our domains
            for d in domains_list:
                if d in subdomain:
                    hits[d] = hits.get(d, 0) + 1
        for d, count in hits.items():
            if count >= RATE_LIMIT_THRESHOLD:
                return d, count
    except Exception:
        pass
    return None, 0

def check_graduation_consecutive(ps):
    """Item 11: Check if health has been above threshold for GRADUATION_CONSECUTIVE days."""
    history = ps.get("spam_rate_history", [])
    if len(history) < GRADUATION_CONSECUTIVE:
        return False
    last_n = history[-GRADUATION_CONSECUTIVE:]
    return all(d.get("spam_rate", 100) < 0.05 for d in last_n)

def send_telegram(message: str):
    try:
        import requests
        token   = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
        if not token or not chat_id:
            return
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": message, "parse_mode": "HTML"},
            timeout=10
        )
    except Exception as e:
        log.warning(f"Telegram alert failed: {e}")

# ── State helpers ─────────────────────────────────────────────
def load_profiles():
    with open(PROFILES_FILE) as f:
        return json.load(f)

def load_state():
    if STATE_FILE.exists():
        with open(STATE_FILE) as f:
            return json.load(f)
    return {}

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

def ensure_profile_state(state, name):
    """Ensure all required keys exist for a profile."""
    defaults = {
        "start_date":        str(date.today()),
        "day":               1,
        "total_sent":        0,
        "status":            "warming",
        "health_score":      50,
        "inbox_count":       0,
        "spam_count":        0,
        "rescue_count":      0,
        "reply_count":       0,
        "spam_rate_history": [],
        "paused":            False,
        "pause_reason":      "",
        "graduated":         False,
        "graduated_date":    "",
    }
    if name not in state:
        state[name] = {}
    for key, val in defaults.items():
        if key not in state[name]:
            state[name][key] = val

def calc_day(start_date_str):
    return (date.today() - date.fromisoformat(start_date_str)).days + 1

# ── Health score ──────────────────────────────────────────────
def calc_health_score(profile_state):
    """
    Score 0-100 based on:
      - Spam rate (0% = 40pts, 5% = 20pts, >20% = 0pts)
      - Reply rate (>10% = 30pts, >5% = 15pts, 0% = 0pts)
      - Rescue count (any rescues = 15pts, many = 30pts)
      - Ramp progress (day/21 * 10pts, max 10pts)
    """
    total_sent   = profile_state.get("total_sent", 0)
    spam_count   = profile_state.get("spam_count", 0)
    reply_count  = profile_state.get("reply_count", 0)
    rescue_count = profile_state.get("rescue_count", 0)
    day          = profile_state.get("day", 1)

    score = 0

    # Spam rate component (max 40pts)
    if total_sent > 0:
        spam_rate = spam_count / total_sent
        if spam_rate == 0:
            score += 40
        elif spam_rate < 0.05:
            score += 30
        elif spam_rate < 0.10:
            score += 20
        elif spam_rate < 0.15:
            score += 10
        else:
            score += 0
    else:
        score += 20  # neutral when no data yet

    # Reply rate component (max 30pts)
    if total_sent > 0:
        reply_rate = reply_count / total_sent
        if reply_rate > 0.10:
            score += 30
        elif reply_rate > 0.05:
            score += 20
        elif reply_rate > 0.02:
            score += 10
        else:
            score += 0

    # Rescue component (max 20pts)
    if rescue_count > 20:
        score += 20
    elif rescue_count > 5:
        score += 15
    elif rescue_count > 0:
        score += 10
    else:
        score += 0

    # Ramp progress (max 10pts)
    ramp_pts = min(10, int((day / GRADUATION_DAY) * 10))
    score += ramp_pts

    return min(100, score)

# ── Auto-pause check ──────────────────────────────────────────
def check_auto_pause(name, profile_state, today_sent, today_spam):
    """
    Auto-pause if spam rate > 20% for AUTO_PAUSE_CONSECUTIVE days.
    Returns True if profile should be paused.
    """
    if today_sent == 0:
        return False

    today_rate = today_spam / today_sent
    history    = profile_state.get("spam_rate_history", [])

    # Add today
    history.append({
        "date":      str(date.today()),
        "spam_rate": round(today_rate, 4),
        "sent":      today_sent,
        "spam":      today_spam,
    })
    # Keep last 7 days only
    history = history[-7:]
    profile_state["spam_rate_history"] = history

    # Check consecutive bad days
    if len(history) >= AUTO_PAUSE_CONSECUTIVE:
        last_n = history[-AUTO_PAUSE_CONSECUTIVE:]
        all_bad = all(d["spam_rate"] > AUTO_PAUSE_SPAM_RATE for d in last_n)
        if all_bad:
            return True

    return False

# ── Seed helpers ──────────────────────────────────────────────
def load_mailboxes():
    with open(MAILBOXES_FILE) as f:
        return json.load(f)

def load_seeds():
    seeds = []
    with open(SEEDS_CSV) as f:
        for row in csv.DictReader(f):
            seeds.append({
                "email":      row["email"].strip(),
                "first_name": row["first_name"].strip() if "first_name" in row else "there"
            })
    return seeds

def pick_seeds(seeds, n):
    if n <= len(seeds):
        return random.sample(seeds, n)
    result, pool = [], seeds[:]
    while len(result) < n:
        random.shuffle(pool)
        result.extend(pool)
    return result[:n]

def write_batch_csv(seeds, body, path):
    fieldnames = ["email","first_name","body_line1","body_line2","closing","sender_name","sender_title"]
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for s in seeds:
            w.writerow({"email": s["email"], "first_name": s["first_name"], **body})

def pick_template():
    templates = list(TEMPLATES_DIR.glob("*.html"))
    return random.choice(templates)

# ── Send from mailbox ─────────────────────────────────────────
def send_from_mailbox(profile, mailbox, seeds, subject, body, day, rate, plog):
    ts        = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_mb   = mailbox.replace("@","_at_").replace(".","_")
    batch_csv = BATCH_DIR / f"{safe_mb}_day{day}_{ts}.csv"
    template  = pick_template()
    write_batch_csv(seeds, body, batch_csv)

    env = os.environ.copy()
    env[f"{profile['name']}_FROM_EMAIL"] = mailbox

    cmd = [
        "python3", SEKI_BIN,
        "--csv",      str(batch_csv),
        "--subject",  subject,
        "--html",     str(template),
        "--profile",  profile["name"],
        "--workers",  "2",
        "--rate",     str(round(rate, 1)),
        "--campaign", f"warmup_{mailbox.split('@')[0]}_day{day}_{ts}",
    ]

    plog.info(f"  → FROM: {mailbox} | {len(seeds)} emails | Rate: {rate}s | Template: {template.name}")
    result = subprocess.run(cmd, capture_output=True, text=True, env=env)

    if result.returncode == 0:
        plog.info(f"  ✅ {mailbox} complete")
        return True
    else:
        plog.error(f"  ❌ {mailbox} error: {result.stderr[:300]}")
        return False

# ── Main ──────────────────────────────────────────────────────
def main():
    log.info("=" * 60)
    log.info(f"Warmup Engine v2 — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    PROFILES  = load_profiles()
    state     = load_state()
    seeds     = load_seeds()
    mailboxes = load_mailboxes()

    log.info(f"Profiles: {len(PROFILES)} | Seeds: {len(seeds)}")

    for p_idx, profile in enumerate(PROFILES):
        name = profile["name"]
        ensure_profile_state(state, name)
        ps = state[name]

        # ── Per-profile logger ────────────────────────────────
        profile_log_file = BASE_DIR / f"warmup_{name.replace(' ','_')}.log"
        plog = make_logger(f"warmup.{name}", profile_log_file)
        # File only — strip StreamHandler to prevent duplicate lines in warmup.log
        plog.handlers = [h for h in plog.handlers if not isinstance(h, logging.StreamHandler)]

        # ── Skip paused ───────────────────────────────────────
        if ps.get("paused"):
            log.info(f"[{name}] PAUSED — {ps.get('pause_reason','')} — skipping")
            plog.info(f"[{name}] PAUSED — skipping this run")
            continue

        # ── Skip graduated ────────────────────────────────────
        if ps.get("graduated"):
            log.info(f"[{name}] GRADUATED on {ps.get('graduated_date','')} — skipping warmup")
            plog.info(f"[{name}] GRADUATED — no longer in warmup")
            continue

        day              = calc_day(ps["start_date"])
        ps["day"]        = day
        volume, rmin, rmax = get_ramp(day)
        mb_list          = mailboxes.get(name, [])

        if not mb_list:
            log.warning(f"[{name}] No mailboxes configured — skipping")
            continue

        plog.info("=" * 50)
        plog.info(f"[{name}] Day {day} | {len(mb_list)} mailboxes | {volume} emails each")

        # ── Inter-profile gap ─────────────────────────────────
        if p_idx > 0:
            gap = random.randint(120, 300) if day <= 3 else random.randint(60, 120)
            log.info(f"[{name}] Waiting {gap}s before this profile...")
            time.sleep(gap)

        profile_total  = 0
        profile_failed = 0
        # Item 10: per-profile cap — check if profile has custom limit in state
        profile_cap    = ps.get("daily_cap_override", None)
        daily_limit    = profile_cap if profile_cap else get_recommended_limit(day)
        plog.info(f"[{name}] Daily cap: {daily_limit} emails{'  (custom)' if profile_cap else ''}")

        for mb_idx, mailbox in enumerate(mb_list):
            if profile_total >= daily_limit:
                plog.info(f"  Daily cap reached ({daily_limit}) — stopping mailbox loop")
                break
            if mb_idx > 0:
                gap = random.randint(30, 90) if day <= 3 else random.randint(15, 45)
                plog.info(f"  Waiting {gap}s before next mailbox...")
                time.sleep(gap)

            subject = random.choice(profile["subjects"])
            body    = random.choice(profile["bodies"])
            rate    = round(random.uniform(rmin, rmax), 1)
            batch   = pick_seeds(seeds, volume)

            if send_from_mailbox(profile, mailbox, batch, subject, body, day, rate, plog):
                profile_total += volume
            else:
                profile_failed += volume

        # ── Update state ──────────────────────────────────────
        ps["total_sent"] += profile_total

        # ── Health score ──────────────────────────────────────
        ps["health_score"] = calc_health_score(ps)

        # ── Gmail 421 rate limit check (item 9) ──────────────────
        rate_domain, rate_hits = check_gmail_rate_limit(name, [get_base_domain(mb) for mb in mb_list[:3]])
        if rate_domain and not ps.get("paused"):
            ps["paused"]       = True
            ps["pause_reason"] = f"Gmail rate limit (421-4.7.28) — {rate_hits} hits on {rate_domain}"
            ps["status"]       = "paused"
            plog.warning(f"[{name}] RATE-LIMITED: {ps['pause_reason']}")
            log.warning(f"[{name}] RATE-LIMITED by Gmail")
            # Clear mailboxes to stop sends immediately
            import json as _jj
            _mf = BASE_DIR / "mailboxes.json"
            _bf = BASE_DIR / "mailboxes_paused_backup.json"
            _mb = _jj.loads(_mf.read_text()) if _mf.exists() else {}
            _bk = _jj.loads(_bf.read_text()) if _bf.exists() else {}
            if _mb.get(name):
                _bk[name] = _mb[name]; _mb[name] = []
                _bf.write_text(_jj.dumps(_bk, indent=2))
                _mf.write_text(_jj.dumps(_mb, indent=2))
            send_telegram(
                f"🚨 <b>Gmail Rate Limit: {name}</b>\n"
                f"Domain: {rate_domain}\n"
                f"Hits: {rate_hits} × 421-4.7.28\n"
                f"Action: Profile auto-paused. Resume from Sentinel after 24h."
            )

        # ── Spam rate auto-pause check ────────────────────────────
        elif check_auto_pause(name, ps, profile_total, ps.get("spam_count", 0)):
            ps["paused"]       = True
            ps["pause_reason"] = f"Spam rate exceeded {AUTO_PAUSE_SPAM_RATE*100:.0f}% for {AUTO_PAUSE_CONSECUTIVE} consecutive days"
            ps["status"]       = "paused"
            plog.warning(f"[{name}] AUTO-PAUSED: {ps['pause_reason']}")
            log.warning(f"[{name}] AUTO-PAUSED")
            send_telegram(
                f"⚠️ <b>Warmup Auto-Paused: {name}</b>\n"
                f"Reason: {ps['pause_reason']}\n"
                f"Day: {day} | Health: {ps['health_score']}\n"
                f"Action: Review spam rates before resuming."
            )
        else:
            ps["status"] = "warming"

        # ── Graduation check (item 11 — consecutive health days) ──
        if (day >= GRADUATION_DAY
                and ps["health_score"] >= GRADUATION_HEALTH
                and check_graduation_consecutive(ps)
                and not ps["graduated"]):
            ps["graduated"]      = True
            ps["graduated_date"] = str(date.today())
            ps["status"]         = "graduated"
            plog.info(f"[{name}] GRADUATED! Day {day} | Health: {ps['health_score']}")
            log.info(f"[{name}] GRADUATED!")
            send_telegram(
                f"🎓 <b>Domain Graduated: {name}</b>\n"
                f"Day: {day} | Health score: {ps['health_score']}/100\n"
                f"Total sent: {ps['total_sent']:,}\n"
                f"Consecutive clean days: {GRADUATION_CONSECUTIVE}\n"
                f"This domain is now warmed up and ready for campaigns."
            )

        plog.info(f"[{name}] Run complete | Today: {profile_total} | Total: {ps['total_sent']} | Health: {ps['health_score']}")
        log.info(f"[{name}] Done | Day {day} | Today: {profile_total} | Health: {ps['health_score']} | Status: {ps['status']}")

        # Save state after each profile
        save_state(state)

    log.info("Warmup Engine v2 complete")
    log.info("=" * 60)

if __name__ == "__main__":
    main()

