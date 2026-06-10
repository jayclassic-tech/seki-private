#!/usr/bin/env python3
"""
Seki Gmail Seed Validator
--------------------------
Tests a Gmail account (email + app password) via IMAP,
then appends it to gmail_seeds.csv if valid.

Usage:
    python3 add_gmail_seed.py
    python3 add_gmail_seed.py --file /opt/seki/warmup/gmail_seeds.csv
"""

import imaplib
import csv
import os
import sys
import argparse
from datetime import datetime
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────────────
DEFAULT_SEEDS_FILE = "/opt/seki/warmup/gmail_seeds.csv"
IMAP_HOST = "imap.gmail.com"
IMAP_PORT = 993

# Expected CSV columns (must match your existing gmail_seeds.csv header)
CSV_COLUMNS = ["email", "password", "imap_host", "imap_port", "added_at"]

# ── Colors (terminal) ─────────────────────────────────────────────────────────
GREEN  = "\033[92m"
RED    = "\033[91m"
YELLOW = "\033[93m"
CYAN   = "\033[96m"
BOLD   = "\033[1m"
RESET  = "\033[0m"

def ok(msg):    print(f"{GREEN}✔ {msg}{RESET}")
def fail(msg):  print(f"{RED}✘ {msg}{RESET}")
def info(msg):  print(f"{CYAN}→ {msg}{RESET}")
def warn(msg):  print(f"{YELLOW}⚠ {msg}{RESET}")


# ── Core: test IMAP login ─────────────────────────────────────────────────────
def test_gmail_imap(email: str, app_password: str) -> tuple[bool, str]:
    """
    Returns (success: bool, message: str)
    Tests actual IMAP SSL login to Gmail.
    """
    try:
        info(f"Connecting to {IMAP_HOST}:{IMAP_PORT} ...")
        mail = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)

        info(f"Logging in as {email} ...")
        mail.login(email, app_password)

        # Quick sanity check — list inbox
        status, _ = mail.select("INBOX")
        mail.logout()

        if status == "OK":
            return True, "Login successful, INBOX accessible."
        else:
            return False, f"Login OK but INBOX select failed (status={status})."

    except imaplib.IMAP4.error as e:
        err = str(e)
        if "AUTHENTICATIONFAILED" in err or "Invalid credentials" in err:
            return False, "Authentication failed. Check the app password is correct and 2FA is on."
        elif "Too many simultaneous connections" in err:
            return False, "Gmail rejected: too many connections on this account right now."
        else:
            return False, f"IMAP error: {err}"

    except OSError as e:
        return False, f"Network error: {e}"

    except Exception as e:
        return False, f"Unexpected error: {e}"


# ── Core: check for duplicates ────────────────────────────────────────────────
def already_exists(seeds_file: str, email: str) -> bool:
    if not Path(seeds_file).exists():
        return False
    with open(seeds_file, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row.get("email", "").strip().lower() == email.strip().lower():
                return True
    return False


# ── Core: append to CSV ───────────────────────────────────────────────────────
def append_to_seeds(seeds_file: str, email: str, app_password: str):
    file_exists = Path(seeds_file).exists()
    file_has_header = False

    if file_exists:
        with open(seeds_file, "r") as f:
            first_line = f.readline().strip()
            file_has_header = first_line.startswith("email")

    with open(seeds_file, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)

        # Write header only if file is new or missing header
        if not file_exists or not file_has_header:
            writer.writeheader()

        writer.writerow({
            "email":     email.strip(),
            "password":  app_password.strip(),
            "imap_host": IMAP_HOST,
            "imap_port": IMAP_PORT,
            "added_at":  datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC"),
        })


# ── Main loop ─────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Validate and add Gmail seeds to Seki warmup.")
    parser.add_argument("--file", default=DEFAULT_SEEDS_FILE,
                        help=f"Path to gmail_seeds.csv (default: {DEFAULT_SEEDS_FILE})")
    args = parser.parse_args()

    seeds_file = args.file

    print(f"\n{BOLD}{'─'*52}")
    print("  Seki Gmail Seed Validator")
    print(f"  Seeds file: {seeds_file}")
    print(f"{'─'*52}{RESET}\n")

    while True:
        # ── Input ──────────────────────────────────────────────────────────
        print(f"{BOLD}Enter Gmail details (or type 'quit' to exit){RESET}")

        email = input("  Gmail address  : ").strip()
        if email.lower() in ("quit", "exit", "q"):
            print("\nBye.")
            break

        if "@" not in email or not email.endswith("gmail.com"):
            warn("That doesn't look like a Gmail address. Try again.\n")
            continue

        app_password = input("  App password   : ").strip()
        # Remove spaces (Google sometimes shows app passwords with spaces)
        app_password_clean = app_password.replace(" ", "")

        if len(app_password_clean) != 16:
            warn(f"App passwords are 16 characters. You entered {len(app_password_clean)}. "
                 f"Make sure it's a Google App Password, not your regular Gmail password.\n")
            cont = input("  Continue anyway? [y/N]: ").strip().lower()
            if cont != "y":
                print()
                continue

        print()

        # ── Duplicate check ────────────────────────────────────────────────
        if already_exists(seeds_file, email):
            warn(f"{email} is already in {seeds_file}. Skipping.\n")
            continue

        # ── Test login ─────────────────────────────────────────────────────
        success, message = test_gmail_imap(email, app_password_clean)

        print()
        if success:
            ok(message)
            append_to_seeds(seeds_file, email, app_password_clean)
            ok(f"Added → {seeds_file}\n")
        else:
            fail(message)
            print()
            print("  Common fixes:")
            print("  1. Make sure 2-Step Verification is ON for the Gmail account")
            print("  2. Generate App Password at: myaccount.google.com/apppasswords")
            print("  3. Use the 16-char app password, NOT the Gmail login password")
            print("  4. Make sure IMAP is enabled in Gmail Settings → See all settings → Forwarding and POP/IMAP\n")

        # ── Ask to add another ─────────────────────────────────────────────
        again = input("Add another? [Y/n]: ").strip().lower()
        if again == "n":
            print("\nDone.")
            break
        print()


if __name__ == "__main__":
    main()
