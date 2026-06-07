#!/usr/bin/env python3
"""
Warmup Engine — Seki Mailer
Rotates through ALL mailboxes per domain on every run.
Each mailbox sends its own batch — warming mailbox, subdomain, and VPS IP.
"""

import os, csv, json, random, logging, subprocess, time
from datetime import datetime, date
from pathlib import Path

BASE_DIR       = Path("/opt/seki/warmup")
SEEDS_CSV      = BASE_DIR / "seeds.csv"
STATE_FILE     = BASE_DIR / "warmup_state.json"
TEMPLATES_DIR  = BASE_DIR / "templates"
BATCH_DIR      = BASE_DIR / "batches"
LOG_FILE       = BASE_DIR / "warmup.log"
MAILBOXES_FILE = BASE_DIR / "mailboxes.json"
SEKI_BIN       = "/opt/seki/postfix_mailer.py"

BATCH_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler()]
)
log = logging.getLogger("warmup")

# (max_day, emails_per_mailbox, rate_min, rate_max)
RAMP = [
    (1,   5,  8.0, 12.0),
    (2,  10,  5.0,  8.0),
    (3,  20,  3.0,  6.0),
    (5,  35,  2.0,  4.0),
    (7,  50,  1.5,  3.0),
    (10, 70,  1.0,  2.0),
    (999,100, 0.5,  1.5),
]

def get_ramp(day):
    for max_day, volume, rmin, rmax in RAMP:
        if day <= max_day:
            return volume, rmin, rmax
    return RAMP[-1][1], RAMP[-1][2], RAMP[-1][3]

PROFILES = [
    {
        "name": "SEKI",
        "subjects": [
            "Quick note for you","Checking in","Following up",
            "Just wanted to reach out","Hello from our team",
            "A message for you","Touching base","We wanted to connect",
        ],
        "bodies": [
            {"body_line1":"Hope this message finds you well. We wanted to reach out and share a quick update.","body_line2":"Feel free to reply if you have any questions — we are always happy to help.","closing":"Have a great day ahead!","sender_name":"Sarah Mitchell","sender_title":"Customer Support, Support Calls Online"},
            {"body_line1":"We appreciate your continued trust and wanted to drop you a quick note.","body_line2":"Our team is here whenever you need assistance. Do not hesitate to reach out.","closing":"Warm regards,","sender_name":"David Okonkwo","sender_title":"Support Team, Support Calls Online"},
            {"body_line1":"Just a friendly hello from the Support Calls Online team.","body_line2":"We are always working to improve your experience. Let us know if there is anything we can do.","closing":"Best wishes,","sender_name":"Linda Chen","sender_title":"Account Relations, Support Calls Online"},
            {"body_line1":"We hope you are having a productive week. A brief note from our support team.","body_line2":"We are available Monday through Friday should you need anything at all.","closing":"All the best,","sender_name":"James Adeyemi","sender_title":"Client Success, Support Calls Online"},
        ]
    },
    {
        "name": "LDGAUTH",
        "subjects": [
            "Your account update","A note from our team","Account activity summary",
            "Quick update for you","Checking in with you","An update from LDG",
            "We wanted to reach out","Your account is active",
        ],
        "bodies": [
            {"body_line1":"We wanted to send you a brief update regarding your account and our latest improvements.","body_line2":"Please feel free to contact our support team if you have any questions or concerns.","closing":"Stay secure,","sender_name":"James Thornton","sender_title":"Account Security, LDG Authenticator"},
            {"body_line1":"This is a routine message from the LDG Authenticator team regarding your account.","body_line2":"Our team is available to assist you with any account-related queries at any time.","closing":"Best regards,","sender_name":"Priya Nair","sender_title":"Support Team, LDG Authenticator"},
            {"body_line1":"We are committed to keeping your account experience smooth and wanted to share a quick note.","body_line2":"If you have any feedback or questions, our team is ready to help.","closing":"Thank you for your trust,","sender_name":"Marcus Evans","sender_title":"Client Services, LDG Authenticator"},
            {"body_line1":"A brief hello from the LDG Authenticator team. We appreciate you being with us.","body_line2":"Do not hesitate to get in touch if there is anything we can assist you with.","closing":"Kind regards,","sender_name":"Amara Osei","sender_title":"Account Relations, LDG Authenticator"},
        ]
    }
    ,{
        "name": "BINANCE",
        "subjects": [
            "Your wallet update","A note from our team","Account activity summary",
            "Quick update for you","Checking in with you","An update from Binance Help",
            "We wanted to reach out","Your account is active",
        ],
        "bodies": [
            {"body_line1":"We wanted to send you a brief update regarding your wallet and our latest security improvements.","body_line2":"Please feel free to contact our support team if you have any questions or concerns.","closing":"Stay secure,","sender_name":"James Thornton","sender_title":"Wallet Security, Binance Help"},
            {"body_line1":"This is a routine message from the Binance Help team regarding your account.","body_line2":"Our team is available to assist you with any wallet or account-related queries at any time.","closing":"Best regards,","sender_name":"Priya Nair","sender_title":"Support Team, Binance Help"},
            {"body_line1":"We are committed to keeping your wallet experience smooth and wanted to share a quick note.","body_line2":"If you have any feedback or questions, our crypto support team is ready to help.","closing":"Thank you for your trust,","sender_name":"Marcus Evans","sender_title":"Client Services, Binance Help"},
            {"body_line1":"A brief hello from the Binance Help team. We appreciate you being with us.","body_line2":"Do not hesitate to get in touch if there is anything we can assist you with regarding your account.","closing":"Kind regards,","sender_name":"Amara Osei","sender_title":"Account Relations, Binance Help"},
        ]
    }
]

def load_state():
    if STATE_FILE.exists():
        with open(STATE_FILE) as f:
            return json.load(f)
    return {p["name"]: {"start_date": str(date.today()), "day": 1, "total_sent": 0} for p in PROFILES}

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

def calc_day(start_date_str):
    return (date.today() - date.fromisoformat(start_date_str)).days + 1

def load_mailboxes():
    with open(MAILBOXES_FILE) as f:
        return json.load(f)

def load_seeds():
    seeds = []
    with open(SEEDS_CSV) as f:
        for row in csv.DictReader(f):
            seeds.append({"email": row["email"].strip(), "first_name": row["first_name"].strip()})
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

def send_from_mailbox(profile, mailbox, seeds, subject, body, day, rate):
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

    log.info(f"  → FROM: {mailbox} | {len(seeds)} emails | Rate: {rate}s | Template: {template.name}")
    result = subprocess.run(cmd, capture_output=True, text=True, env=env)

    if result.returncode == 0:
        log.info(f"  ✅ {mailbox} complete")
    else:
        log.error(f"  ❌ {mailbox} error: {result.stderr[:300]}")
    return result.returncode == 0

def main():
    log.info("=" * 60)
    log.info(f"Warmup Engine — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    state     = load_state()
    seeds     = load_seeds()
    mailboxes = load_mailboxes()

    log.info(f"Loaded {len(seeds)} seed addresses")

    for p_idx, profile in enumerate(PROFILES):
        name = profile["name"]

        if name not in state:
            state[name] = {"start_date": str(date.today()), "day": 1, "total_sent": 0}

        day              = calc_day(state[name]["start_date"])
        volume, rmin, rmax = get_ramp(day)
        mb_list          = mailboxes.get(name, [])

        if not mb_list:
            log.warning(f"[{name}] No mailboxes configured — skipping")
            continue

        log.info(f"[{name}] Day {day} | {len(mb_list)} mailboxes | {volume} emails each")

        if p_idx > 0:
            gap = random.randint(120, 300) if day <= 3 else random.randint(60, 120)
            log.info(f"[{name}] Waiting {gap}s before this profile...")
            time.sleep(gap)

        profile_total = 0

        for mb_idx, mailbox in enumerate(mb_list):
            if mb_idx > 0:
                gap = random.randint(30, 90) if day <= 3 else random.randint(15, 45)
                log.info(f"  Waiting {gap}s before next mailbox...")
                time.sleep(gap)

            subject = random.choice(profile["subjects"])
            body    = random.choice(profile["bodies"])
            rate    = round(random.uniform(rmin, rmax), 1)
            batch   = pick_seeds(seeds, volume)

            if send_from_mailbox(profile, mailbox, batch, subject, body, day, rate):
                profile_total += volume

        state[name]["total_sent"] += profile_total
        log.info(f"[{name}] Done | Today: {profile_total} | Total: {state[name]['total_sent']}")

    save_state(state)
    log.info("Warmup Engine complete")
    log.info("=" * 60)

if __name__ == "__main__":
    main()
