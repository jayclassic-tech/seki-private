#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════════╗
║              SEKI MAILER — Direct SMTP via Postfix               ║
║              Version 2.3 | Optimized & Production-Ready          ║
╠══════════════════════════════════════════════════════════════════╣
║  Features:                                                        ║
║    ✦ VERP bounce handling  (track exactly who bounced)           ║
║    ✦ Multi-profile architecture  (switch identity per campaign)  ║
║    ✦ Thread-safe subdomain round-robin  (spread sending load)    ║
║    ✦ Dry-run mode  (simulate without delivering)                 ║
║    ✦ List-Unsubscribe-Post headers  (RFC 8058 one-click unsub)   ║
║    ✦ Plain-text auto-generation  (from HTML, always)             ║
║    ✦ Suppression list  (never re-send to bounces/complaints)     ║
║    ✦ Retry logic  (transient SMTP failures recovered)            ║
║    ✦ Rate limiting  (control send speed per thread)              ║
║    ✦ Template engine  ({{variable}} placeholders in HTML/subject)║
║    ✦ SQLite send log  (permanent record of every send)           ║
║    ✦ Bounce mailbox parser  (IMAP → auto-suppress bounced addrs) ║
║    ✦ Sentinel integration  (POST campaign stats to your panel)   ║
║    ✦ Telegram alerts  (campaign summary to your bot)             ║
╚══════════════════════════════════════════════════════════════════╝

Usage:
    python postfix_mailer.py \
        --csv recipients.csv \
        --subject "Hello {{first_name}}" \
        --html template.html \
        --dry-run

Environment (.env file or shell exports):
    SEKI_FROM_NAME="Support Team"
    SEKI_FROM_EMAIL="support@mail.yourdomain.com"
    SEKI_REPLY_TO="help@yourdomain.com"
    SEKI_SUBDOMAINS="mail,send,news"          # comma-separated
    SEKI_BOUNCE_DOMAIN="mail.yourdomain.com"  # for VERP
    SEKI_VERP_SECRET="your-secret-key"
    SEKI_UNSUB_EMAIL="unsub@yourdomain.com"
    SEKI_UNSUB_URL="https://yourdomain.com/unsubscribe"
"""

# ── Standard Library ───────────────────────────────────────────────────────────
import os
import re
import csv
import time
import hmac
import uuid
import imaplib
import email as email_lib
import sqlite3
import logging
import smtplib
import hashlib
import argparse
import threading
import base64
import json
import urllib.request
import urllib.error
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr, formatdate, make_msgid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional

# ── Third-party ────────────────────────────────────────────────────────────────
try:
    import html2text
    HTML2TEXT_AVAILABLE = True
except ImportError:
    HTML2TEXT_AVAILABLE = False

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # .env loading is optional; use shell exports


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 1: LOGGING
# ─────────────────
# Two handlers: one prints to terminal, one writes to a file.
# This means you always have a permanent send log on disk.
# ══════════════════════════════════════════════════════════════════════════════

def _setup_logger() -> logging.Logger:
    fmt = "%(asctime)s [%(levelname)-8s] %(threadName)-12s | %(message)s"
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    try:
        handlers.append(logging.FileHandler("seki_mailer.log", encoding="utf-8"))
    except PermissionError:
        pass
    logging.basicConfig(level=logging.INFO, format=fmt, handlers=handlers)
    return logging.getLogger("seki")


log = _setup_logger()


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 2: SUBDOMAIN ROUND-ROBIN (THREAD-SAFE)
# ────────────────────────────────────────────────
# WHY: Spreading sends across multiple subdomains (mail1., mail2., etc.)
#       reduces per-subdomain sending volume, which helps with:
#       • IP warm-up (each sub has its own reputation bucket)
#       • Rate-limit avoidance at receiving MX servers
#       • Isolating damage if one subdomain gets flagged
#
# WHY threading.Lock: Multiple threads will call .next() simultaneously.
#       Without a lock, two threads could read the same index at the same
#       time → duplicate subdomains. The lock ensures only one thread
#       advances the counter at a time.
# ══════════════════════════════════════════════════════════════════════════════

class SubdomainRotator:
    """Thread-safe round-robin rotator across a list of sending subdomains."""

    def __init__(self, subdomains: list[str]):
        if not subdomains or not subdomains[0]:
            raise ValueError("SubdomainRotator requires at least one non-empty subdomain.")
        self._subdomains = subdomains
        self._index = 0
        self._lock = threading.Lock()

    def next(self) -> str:
        with self._lock:                           # ← only one thread at a time
            sub = self._subdomains[self._index % len(self._subdomains)]
            self._index += 1
            return sub

    def __repr__(self) -> str:
        return f"SubdomainRotator({self._subdomains})"


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 3: VERP (Variable Envelope Return Path)
# ────────────────────────────────────────────────
# WHY VERP: When an email bounces, the bounce report goes to the Return-Path
#            (envelope sender). Without VERP, all bounces go to one address
#            and you have to parse bounce bodies (fragile) to find who bounced.
#
#            With VERP, you encode the recipient INTO the envelope sender:
#            bounces+jabari=gmail.com@mail.yourdomain.com
#
#            When gmail.com bounces, the bounce email goes to THAT unique
#            address → your MTA knows EXACTLY who bounced → suppress them.
#
# HMAC TAG (optional): Adds a short cryptographic signature to the VERP
#            address so you can verify bounces are genuine and not forged.
#
#  VERP address format:
#  bounces + <local> = <domain> + <hmac_tag> @ <bounce_domain>
#  Example:
#  bounces+jabari=gmail.com+aB3xYz12@mail.supportcallsonline.com
# ══════════════════════════════════════════════════════════════════════════════

def build_verp_sender(recipient: str, bounce_domain: str, secret: str = "") -> str:
    """
    Encode recipient info into the envelope Return-Path (VERP).

    Args:
        recipient:     The To: address  (e.g. jabari@gmail.com)
        bounce_domain: Your sending domain  (e.g. mail.supportcallsonline.com)
        secret:        HMAC secret for tag generation (optional but recommended)

    Returns:
        VERP address like: bounces+jabari=gmail.com+aB3x@mail.yourdomain.com
    """
    if "@" not in recipient:
        raise ValueError(f"Invalid recipient address: {recipient}")
    local, domain = recipient.lower().split("@", 1)
    verp_local = f"bounces+{local}={domain}"
    if secret:
        tag = _hmac_short(recipient.lower(), secret)
        verp_local = f"{verp_local}+{tag}"
    return f"{verp_local}@{bounce_domain}"


def parse_verp_sender(verp_address: str) -> Optional[str]:
    """
    Decode a VERP address back to the original recipient.

    Args:
        verp_address: e.g. bounces+jabari=gmail.com@mail.yourdomain.com

    Returns:
        Original recipient (jabari@gmail.com) or None if not a VERP address.
    """
    pattern = r"bounces\+([^=]+)=([^+@]+)(?:\+\w+)?@"
    match = re.match(pattern, verp_address)
    return f"{match.group(1)}@{match.group(2)}" if match else None


def _hmac_short(email: str, secret: str, length: int = 8) -> str:
    """Generate a short URL-safe HMAC tag for VERP verification."""
    digest = hmac.new(secret.encode(), email.encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest)[:length].decode()


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 4: HTML → PLAIN TEXT AUTO-GENERATION
# ─────────────────────────────────────────────
# WHY: Every marketing email MUST have a plain-text (text/plain) part.
#       Without it:
#       • Gmail/Outlook may mark as spam (looks like a phishing template)
#       • Spam filters penalize HTML-only messages
#       • Accessibility: screen readers, plain-text email clients
#
# The MIMEMultipart("alternative") structure means the email client
# picks the best version it can render (prefers HTML, falls back to text).
# ══════════════════════════════════════════════════════════════════════════════

def html_to_plain(html: str) -> str:
    """
    Convert HTML email body to clean plain text.
    Falls back to simple regex stripping if html2text is unavailable.
    """
    if HTML2TEXT_AVAILABLE:
        converter = html2text.HTML2Text()
        converter.ignore_links = False      # keep URLs visible in plain text
        converter.ignore_images = True      # skip [image] noise
        converter.body_width = 80           # wrap at 80 chars
        converter.protect_links = True      # don't break URLs
        return converter.handle(html).strip()

    # ── Fallback: strip HTML tags ──────────────────────────────────────────
    text = re.sub(r"<br\s*/?>", "\n", html, flags=re.IGNORECASE)
    text = re.sub(r"<p[^>]*>", "\n\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"&lt;", "<", text)
    text = re.sub(r"&gt;", ">", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 5: TEMPLATE ENGINE
# ───────────────────────────
# WHY: Personalisation dramatically improves open rates and reduces spam
#       scoring (templated emails look less like blasts).
#
# Syntax: {{variable_name}} in HTML/subject is replaced with the value
#          from the recipient's CSV row. Case-sensitive.
#
# Example:
#   Template: "Hi {{first_name}}, your wallet address is {{wallet}}"
#   CSV row:  {"email": "...", "first_name": "Jabari", "wallet": "0x..."}
#   Output:   "Hi Jabari, your wallet address is 0x..."
#
# Safety: Unknown placeholders (no matching key) are left as-is and
#          logged as a warning so you notice missing data.
# ══════════════════════════════════════════════════════════════════════════════

_PLACEHOLDER_RE = re.compile(r"\{\{(\w+)\}\}")


def render(template: str, variables: dict) -> str:
    """Replace {{key}} placeholders with values from variables dict."""
    def replace(match: re.Match) -> str:
        key = match.group(1)
        if key not in variables:
            log.debug(f"Template placeholder '{{{{{{key}}}}}}' has no value — left as-is")
            return match.group(0)
        return str(variables[key])
    return _PLACEHOLDER_RE.sub(replace, template)


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 6: SENDER PROFILE
# ──────────────────────────
# WHY: A "profile" is a complete sending identity. Having multiple profiles
#       means you can:
#       • Send supportcallsonline.com campaigns from profile A
#       • Send cryptosupportagency.com campaigns from profile B
#       • Switch with just an env var change (no code edits)
#
# Profile is loaded from .env (or environment variables) using a PREFIX.
# Default prefix = "SEKI". You can have SUPPORT_*, CRYPTO_* profiles etc.
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class SenderProfile:
    """
    Complete sending identity for one domain/campaign type.

    Attributes:
        name            Friendly label for this profile (logging only)
        from_name       Display name in From: header  ("Support Team")
        from_email      Base sending address  (support@mail.yourdomain.com)
        reply_to        Where replies go (can differ from from_email)
        subdomains      List of sending subdomains for round-robin rotation
        bounce_domain   Domain to receive VERP bounces
        verp_secret     HMAC secret — sign VERP addresses to prevent forgery
        unsubscribe_email  mailto: address for List-Unsubscribe
        unsubscribe_url    HTTPS endpoint for one-click unsubscribe (RFC 8058)
    """
    name: str
    from_name: str
    from_email: str
    reply_to: Optional[str] = None
    subdomains: list[str] = field(default_factory=list)
    bounce_domain: str = ""
    verp_secret: str = ""
    unsubscribe_email: str = ""
    unsubscribe_url: str = ""

    @classmethod
    def from_env(cls, prefix: str = "SEKI") -> "SenderProfile":
        """
        Load profile from environment variables.

        All vars follow the pattern: <PREFIX>_<KEY>
        Example: SEKI_FROM_NAME, SEKI_FROM_EMAIL, SEKI_SUBDOMAINS
        """
        raw_subs = os.getenv(f"{prefix}_SUBDOMAINS", "")
        subdomains = [s.strip() for s in raw_subs.split(",") if s.strip()]

        profile = cls(
            name=os.getenv(f"{prefix}_PROFILE_NAME", prefix.lower()),
            from_name=os.getenv(f"{prefix}_FROM_NAME", "Support Team"),
            from_email=os.getenv(f"{prefix}_FROM_EMAIL", ""),
            reply_to=os.getenv(f"{prefix}_REPLY_TO") or None,
            subdomains=subdomains,
            bounce_domain=os.getenv(f"{prefix}_BOUNCE_DOMAIN", ""),
            verp_secret=os.getenv(f"{prefix}_VERP_SECRET", ""),
            unsubscribe_email=os.getenv(f"{prefix}_UNSUB_EMAIL", ""),
            unsubscribe_url=os.getenv(f"{prefix}_UNSUB_URL", ""),
        )

        if not profile.from_email:
            raise EnvironmentError(
                f"Missing required env var: {prefix}_FROM_EMAIL\n"
                f"Add it to your .env file or export it in your shell."
            )

        log.info(
            f"Profile loaded: [{profile.name}] "
            f"From={profile.from_email} | Subdomains={profile.subdomains or 'none'}"
        )
        return profile


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 7: SUPPRESSION LIST
# ────────────────────────────
# WHY: Sending to addresses that previously bounced or unsubscribed:
#       • Destroys your sender reputation
#       • Can get you blacklisted by Spamhaus/Barracuda
#       • Is illegal in many jurisdictions (CAN-SPAM, GDPR)
#
# The suppression list is a flat file (one email per line).
# It's loaded into memory at startup for O(1) lookup on every send.
# New suppressions are appended to the file immediately (thread-safe).
#
# Feed bounces into it by parsing VERP bounce mailbox or webhook.
# ══════════════════════════════════════════════════════════════════════════════

class SuppressionList:
    """
    Thread-safe persistent suppression list.
    Stored as a UTF-8 text file, one lowercase email per line.
    """

    def __init__(self, filepath: str = "suppressed.txt"):
        self._path = Path(filepath)
        self._emails: set[str] = set()
        self._lock = threading.Lock()
        self._load()

    def _load(self):
        if self._path.exists():
            lines = self._path.read_text(encoding="utf-8").splitlines()
            self._emails = {ln.strip().lower() for ln in lines if ln.strip()}
            log.info(f"Suppression list: {len(self._emails)} addresses loaded from {self._path}")
        else:
            log.info(f"Suppression list: {self._path} not found — starting fresh")

    def is_suppressed(self, email: str) -> bool:
        """O(1) set lookup — fast even with millions of entries."""
        return email.strip().lower() in self._emails

    def add(self, email: str):
        """Add email to suppression list (in-memory + file)."""
        normalised = email.strip().lower()
        with self._lock:
            if normalised not in self._emails:
                self._emails.add(normalised)
                with open(self._path, "a", encoding="utf-8") as f:
                    f.write(normalised + "\n")
                log.info(f"Suppressed: {normalised}")

    def add_bulk(self, emails: list[str]):
        for e in emails:
            self.add(e)

    def __len__(self) -> int:
        return len(self._emails)


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 8: STATS TRACKER
# ─────────────────────────
# Tracks the outcome of every send across all threads.
# Uses a lock so concurrent thread increments don't corrupt counts.
# ══════════════════════════════════════════════════════════════════════════════

class Stats:
    """Thread-safe send statistics."""
    __slots__ = ("sent", "failed", "skipped", "dry_run", "retried", "_lock")

    def __init__(self):
        self.sent = 0
        self.failed = 0
        self.skipped = 0
        self.dry_run = 0
        self.retried = 0
        self._lock = threading.Lock()

    def inc(self, stat: str, by: int = 1):
        with self._lock:
            setattr(self, stat, getattr(self, stat) + by)

    def summary(self) -> str:
        total = self.sent + self.failed + self.skipped + self.dry_run
        return (
            f"\n{'─'*50}\n"
            f"  📊 SEKI MAILER — CAMPAIGN SUMMARY\n"
            f"{'─'*50}\n"
            f"  ✅ Sent        : {self.sent}\n"
            f"  ❌ Failed      : {self.failed}\n"
            f"  ⏭  Skipped     : {self.skipped}  (suppressed)\n"
            f"  🔍 Dry-run     : {self.dry_run}\n"
            f"  🔁 Retried     : {self.retried}\n"
            f"  📬 Total       : {total}\n"
            f"{'─'*50}"
        )


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 9: CORE MAILER
# ───────────────────────
# This is where everything comes together. SekiMailer:
#
#   1. Picks a sending subdomain via round-robin
#   2. Builds VERP return-path for the envelope
#   3. Constructs a complete MIME message (HTML + auto plain-text)
#   4. Adds List-Unsubscribe + List-Unsubscribe-Post headers
#   5. Sends via localhost:25 (Postfix) with retry on transient failures
#   6. Tracks stats + respects suppression list
#   7. Supports dry-run mode (all steps except actual SMTP DATA)
#   8. Supports bulk sends via a ThreadPoolExecutor
# ══════════════════════════════════════════════════════════════════════════════

# SMTP reply codes that mean "try again later" (transient failures)
_TRANSIENT_SMTP_CODES = {421, 450, 451, 452}

class SekiMailer:
    """
    Direct email sender via Postfix on localhost:25.

    Args:
        profile      : SenderProfile — who is sending
        smtp_host    : SMTP host (default localhost)
        smtp_port    : SMTP port (default 25)
        dry_run      : If True, build messages but don't send
        max_workers  : Number of concurrent sending threads
        rate_limit   : Seconds to wait between sends per thread
        max_retries  : How many times to retry on transient SMTP errors
        retry_delay  : Base seconds to wait before retry (doubles each attempt)
        suppression  : SuppressionList instance (created if not passed)
    """

    def __init__(
        self,
        profile: SenderProfile,
        smtp_host: str = "127.0.0.1",
        smtp_port: int = 25,
        dry_run: bool = False,
        max_workers: int = 5,
        rate_limit: float = 0.1,
        max_retries: int = 3,
        retry_delay: float = 2.0,
        suppression: Optional[SuppressionList] = None,
        send_log: Optional["SendLog"] = None,
        campaign_id: Optional[str] = None,
        sentinel: Optional["SentinelReporter"] = None,
        telegram: Optional["TelegramNotifier"] = None,
    ):
        self.profile = profile
        self.smtp_host = smtp_host
        self.smtp_port = smtp_port
        self.dry_run = dry_run
        self.max_workers = max_workers
        self.rate_limit = rate_limit
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self.suppression = suppression or SuppressionList()
        self.send_log = send_log
        self.sentinel = sentinel
        self.telegram = telegram
        # campaign_id groups all sends in this run together in the DB
        # defaults to  profile_name + UTC timestamp  so it's always unique
        self.campaign_id = campaign_id or (
            f"{profile.name}_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"
        )
        self.stats = Stats()
        self._rotator = (
            SubdomainRotator(profile.subdomains)
            if profile.subdomains
            else None
        )

        log.info(
            f"SekiMailer ready | Profile={profile.name} | "
            f"SMTP={smtp_host}:{smtp_port} | Workers={max_workers} | "
            f"DryRun={dry_run} | Campaign={self.campaign_id}"
        )

    # ── Private: resolve From address with subdomain rotation ────────────────
    def _resolved_from_email(self) -> str:
        """
        Builds the actual From address by injecting the next subdomain.

        Example:
            from_email = "support@mail.yourdomain.com"
            subdomains = ["mail1", "mail2", "send"]
            rotator picks "mail1"
            result = "support@mail1.yourdomain.com"

        IMPORTANT: The subdomain replaces the FIRST label of the mail domain.
        So your Postfix + DNS must have each subdomain configured with
        its own SPF and DKIM records.
        """
        if not self._rotator:
            return self.profile.from_email

        subdomain = self._rotator.next()
        local, mail_domain = self.profile.from_email.split("@", 1)
        # Replace first label: mail.yourdomain.com → subdomain.yourdomain.com
        parts = mail_domain.split(".", 1)
        base = parts[1] if len(parts) == 2 else mail_domain
        return f"{local}@{subdomain}.{base}"

    # ── Private: build MIME message ──────────────────────────────────────────
    def _build(
        self,
        recipient: str,
        subject: str,
        html_body: str,
        variables: dict,
        plain_body: Optional[str] = None,
    ) -> MIMEMultipart:
        """
        Construct a complete RFC-compliant MIME email message.

        The message structure is multipart/alternative:
            └── text/plain   (shown by plain-text clients, spam filters love it)
            └── text/html    (shown by modern clients, richer layout)
        text/plain is attached FIRST — MIME spec says clients prefer the LAST
        part they can render, so HTML (last) is preferred when supported.
        """
        # 1. Render templates
        subject_rendered = render(subject, variables)
        html_rendered = render(html_body, variables)
        text_rendered = render(plain_body, variables) if plain_body else html_to_plain(html_rendered)

        # 2. Resolve From address (uses subdomain rotator)
        from_email = self._resolved_from_email()

        # 3. Build VERP envelope sender
        #    NOTE: This is the SMTP envelope sender (MAIL FROM:), NOT the From: header.
        #    They are intentionally different.
        if self.profile.bounce_domain:
            envelope_sender = build_verp_sender(
                recipient=recipient,
                bounce_domain=self.profile.bounce_domain,
                secret=self.profile.verp_secret,
            )
        else:
            envelope_sender = from_email

        # 4. Assemble MIME object
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject_rendered
        msg["From"] = formataddr((self.profile.from_name, from_email))
        msg["To"] = recipient
        msg["Date"] = formatdate(localtime=True)
        msg["Message-ID"] = make_msgid(domain=from_email.split("@")[-1])
        msg["X-Mailer"] = "SekiMailer/2.0"
        msg["Precedence"] = "bulk"

        # 5. Reply-To
        if self.profile.reply_to:
            msg["Reply-To"] = self.profile.reply_to

        # 6. List-Unsubscribe headers
        #
        #    List-Unsubscribe: <https://...>, <mailto:...>
        #      → Shows "Unsubscribe" link in Gmail/Outlook header bar
        #      → Required by Gmail & Yahoo for bulk senders (2024 policy)
        #
        #    List-Unsubscribe-Post: List-Unsubscribe=One-Click
        #      → Enables one-click unsubscribe (RFC 8058)
        #      → Gmail/Yahoo will send a POST to your URL automatically
        #      → Without this, Gmail shows a manual unsubscribe warning
        #
        unsub_parts = []
        if self.profile.unsubscribe_url:
            unsub_parts.append(f"<{self.profile.unsubscribe_url}>")
        if self.profile.unsubscribe_email:
            unsub_parts.append(f"<mailto:{self.profile.unsubscribe_email}>")
        if unsub_parts:
            msg["List-Unsubscribe"] = ", ".join(unsub_parts)
            msg["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"

        # 7. Attach parts (plain FIRST, HTML LAST — client prefers last renderable)
        msg.attach(MIMEText(text_rendered, "plain", "utf-8"))
        msg.attach(MIMEText(html_rendered, "html", "utf-8"))

        # 8. Stash envelope sender on the object (used in _send_one)
        #    MIMEMultipart is a dict-like, storing as private attr is clean
        msg._seki_envelope_sender = envelope_sender

        return msg

    # ── Private: transmit one message via SMTP ───────────────────────────────
    def _send_one(self, recipient: str, msg: MIMEMultipart) -> bool:
        """
        Connect to Postfix on localhost:25 and deliver one message.

        Key point: smtplib.SMTP connects, sends EHLO, then we call sendmail()
        which sends MAIL FROM: (VERP address), RCPT TO: (recipient), DATA.

        Retry logic:
        - Transient errors (4xx codes): wait and retry up to max_retries
        - Permanent errors (5xx codes): fail immediately, do NOT retry
        """
        if self.dry_run:
            log.info(
                f"[DRY-RUN] To={recipient} | "
                f"From={msg['From']} | Envelope={msg._seki_envelope_sender}"
            )
            self.stats.inc("dry_run")
            return True

        attempt = 0
        delay = self.retry_delay

        while attempt <= self.max_retries:
            try:
                with smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=15) as smtp:
                    smtp.sendmail(
                        from_addr=msg._seki_envelope_sender,
                        to_addrs=[recipient],
                        msg=msg.as_bytes(),
                    )
                log.info(
                    f"✅ SENT | To={recipient} | "
                    f"MsgID={msg['Message-ID']} | From={msg['From']}"
                )
                self.stats.inc("sent")
                return True

            except smtplib.SMTPRecipientsRefused as e:
                # Permanent — the address was flat-out rejected (5xx)
                log.warning(f"❌ REFUSED | To={recipient} | Reason={e.recipients}")
                break  # no retry

            except smtplib.SMTPResponseException as e:
                if e.smtp_code in _TRANSIENT_SMTP_CODES:
                    attempt += 1
                    self.stats.inc("retried")
                    log.warning(
                        f"⚠ TRANSIENT | To={recipient} | "
                        f"Code={e.smtp_code} | Retry {attempt}/{self.max_retries} in {delay}s"
                    )
                    if attempt <= self.max_retries:
                        time.sleep(delay)
                        delay *= 2  # exponential back-off
                    continue
                else:
                    # Permanent SMTP error
                    log.error(f"❌ SMTP ERROR | To={recipient} | {e.smtp_code}: {e.smtp_error}")
                    break

            except (smtplib.SMTPException, ConnectionRefusedError, OSError) as e:
                log.error(f"❌ CONNECTION ERROR | To={recipient} | {e}")
                break

        self.stats.inc("failed")
        return False

    # ── Public: send to a single recipient ──────────────────────────────────
    def send(
        self,
        recipient: str,
        subject: str,
        html_body: str,
        variables: Optional[dict] = None,
        plain_body: Optional[str] = None,
    ) -> bool:
        """
        Send one email to one recipient.

        Args:
            recipient   : Target email address
            subject     : Subject line (supports {{variables}})
            html_body   : HTML email body (supports {{variables}})
            variables   : Dict of template substitutions (e.g. {"first_name": "Jabari"})
            plain_body  : Optional plain-text. Auto-generated from HTML if omitted.

        Returns:
            True on success, False on failure/skip.
        """
        variables = variables or {}
        recipient = recipient.strip()

        if not recipient:
            log.warning("Empty recipient — skipping")
            return False

        # Suppression check before building/sending anything
        if self.suppression.is_suppressed(recipient):
            log.info(f"⏭ SUPPRESSED | {recipient}")
            self.stats.inc("skipped")
            if self.send_log:
                self.send_log.record(
                    campaign_id=self.campaign_id,
                    recipient=recipient,
                    subject=subject,
                    status="skipped",
                    profile=self.profile.name,
                )
            return False

        msg = self._build(recipient, subject, html_body, variables, plain_body)
        result = self._send_one(recipient, msg)

        # Record outcome in SQLite
        if self.send_log:
            self.send_log.record(
                campaign_id=self.campaign_id,
                recipient=recipient,
                subject=msg["Subject"],
                from_email=msg["From"],
                envelope=getattr(msg, "_seki_envelope_sender", ""),
                message_id=msg.get("Message-ID", ""),
                status="dry_run" if self.dry_run else ("sent" if result else "failed"),
                profile=self.profile.name,
            )

        # Rate limit: sleep AFTER each send to pace delivery
        if self.rate_limit > 0:
            time.sleep(self.rate_limit)

        return result

    # ── Public: bulk send from a list of recipient dicts ────────────────────
    def send_bulk(
        self,
        recipients: list[dict],
        subject: str,
        html_body: str,
        plain_body: Optional[str] = None,
    ) -> Stats:
        """
        Send the same campaign to a list of recipients concurrently.

        Each dict in `recipients` must have an "email" key.
        All other keys are available as {{variable}} template values.

        Example recipients list:
            [
                {"email": "a@example.com", "first_name": "Alice"},
                {"email": "b@example.com", "first_name": "Bob"},
            ]

        Args:
            recipients  : List of dicts (from CSV or direct)
            subject     : Subject template
            html_body   : HTML body template
            plain_body  : Optional plain-text template

        Returns:
            Stats object with full send summary.
        """
        total = len(recipients)
        campaign_started_at = datetime.now(timezone.utc).isoformat()
        log.info(
            f"\n{'═'*50}\n"
            f"  🚀 CAMPAIGN START\n"
            f"  Recipients : {total}\n"
            f"  Workers    : {self.max_workers}\n"
            f"  Rate limit : {self.rate_limit}s/send\n"
            f"  Dry-run    : {self.dry_run}\n"
            f"  Profile    : {self.profile.name}\n"
            f"{'═'*50}"
        )

        def _task(row: dict):
            email = row.get("email", "").strip()
            if not email:
                log.warning(f"Row missing 'email' field: {row}")
                self.stats.inc("skipped")
                return
            self.send(email, subject, html_body, variables=row, plain_body=plain_body)

        with ThreadPoolExecutor(
            max_workers=self.max_workers,
            thread_name_prefix="seki-worker",
        ) as executor:
            futures = {executor.submit(_task, row): row for row in recipients}
            for future in as_completed(futures):
                exc = future.exception()
                if exc:
                    log.error(f"Unhandled thread exception: {exc}")

        log.info(self.stats.summary())

        # ── Report to Sentinel panel (non-blocking, failure is silent) ────────
        if self.sentinel:
            self.sentinel.report(
                campaign_id=self.campaign_id,
                profile=self.profile.name,
                stats=self.stats,
                subject=subject,
                started_at=campaign_started_at,
                dry_run_mode=self.dry_run,
            )

        # ── Send Telegram alert (non-blocking, failure is silent) ─────────────
        if self.telegram:
            # Calculate duration
            try:
                t_start = datetime.fromisoformat(campaign_started_at)
                t_end   = datetime.now(timezone.utc)
                duration_sec = int((t_end - t_start).total_seconds())
            except Exception:
                duration_sec = 0
            self.telegram.notify(
                campaign_id=self.campaign_id,
                profile=self.profile.name,
                stats=self.stats,
                subject=subject,
                duration_sec=duration_sec,
                dry_run_mode=self.dry_run,
            )

        return self.stats

    # ── Public: load recipients from a CSV file ──────────────────────────────
    @staticmethod
    def load_csv(filepath: str) -> list[dict]:
        """
        Load recipient list from a CSV file.

        Required columns: email
        Recommended:      first_name, last_name, (any template variables you use)

        Example CSV:
            email,first_name,wallet
            jabari@example.com,Jabari,0xABC123
            alice@example.com,Alice,0xDEF456
        """
        path = Path(filepath)
        if not path.exists():
            raise FileNotFoundError(f"Recipients CSV not found: {filepath}")

        recipients = []
        with open(path, newline="", encoding="utf-8-sig") as f:  # utf-8-sig strips BOM
            reader = csv.DictReader(f)
            for row in reader:
                row = {k.strip(): v.strip() for k, v in row.items() if k}
                recipients.append(row)

        log.info(f"📂 CSV loaded: {len(recipients)} recipients from {filepath}")
        return recipients


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 11: SQLITE SEND LOG
# ════════════════════════════
# WHY SQLite over a plain text/CSV log:
#   • Queryable: "show me all failed sends from campaign X" → one SQL line
#   • Atomic writes: SQLite handles concurrent threads safely via WAL mode
#   • No external dependency: built into Python, zero install needed
#   • Small footprint: millions of rows fit in a few MB
#
# DATABASE SCHEMA — table: sends
# ─────────────────────────────────────────────────────────────────────────────
#  send_id     TEXT  PK  Unique ID per send (UUID4)
#  campaign_id TEXT      Groups related sends (timestamp-based by default)
#  recipient   TEXT      To: address
#  subject     TEXT      Rendered subject line
#  from_email  TEXT      Actual From address used (with subdomain)
#  envelope    TEXT      VERP envelope sender used (MAIL FROM:)
#  message_id  TEXT      SMTP Message-ID header value
#  status      TEXT      'sent' | 'failed' | 'skipped' | 'dry_run'
#  error       TEXT      Error detail if status='failed' (else NULL)
#  profile     TEXT      Profile name (e.g. 'supportcallsonline')
#  sent_at     TEXT      ISO 8601 UTC timestamp
#
# HOW IT FITS IN:
#   SekiMailer creates one SendLog instance → passes it to each send call →
#   every outcome (sent/failed/skipped) is written immediately.
#   You can query the DB file from any SQLite client (DB Browser, sqlite3 CLI).
# ══════════════════════════════════════════════════════════════════════════════

class SendLog:
    """
    Thread-safe SQLite logger for every send attempt.

    Usage:
        log_db = SendLog("seki_sends.db")
        log_db.record(campaign_id="camp_001", recipient="a@b.com", ...)
        rows = log_db.query("SELECT * FROM sends WHERE status='failed'")
    """

    _CREATE_TABLE = """
        CREATE TABLE IF NOT EXISTS sends (
            send_id     TEXT PRIMARY KEY,
            campaign_id TEXT NOT NULL,
            recipient   TEXT NOT NULL,
            subject     TEXT,
            from_email  TEXT,
            envelope    TEXT,
            message_id  TEXT,
            status      TEXT NOT NULL,
            error       TEXT,
            profile     TEXT,
            sent_at     TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_campaign ON sends (campaign_id);
        CREATE INDEX IF NOT EXISTS idx_recipient ON sends (recipient);
        CREATE INDEX IF NOT EXISTS idx_status    ON sends (status);
    """

    def __init__(self, db_path: str = "seki_sends.db"):
        self._path = db_path
        self._lock = threading.Lock()
        # check_same_thread=False is safe because we use our own lock above
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL;")  # WAL = safe concurrent writes
        self._conn.executescript(self._CREATE_TABLE)
        self._conn.commit()
        log.info(f"SendLog: database ready → {db_path}")

    def record(
        self,
        *,
        campaign_id: str,
        recipient: str,
        subject: str = "",
        from_email: str = "",
        envelope: str = "",
        message_id: str = "",
        status: str,           # 'sent' | 'failed' | 'skipped' | 'dry_run'
        error: str = "",
        profile: str = "",
    ):
        """Insert one send record. Thread-safe."""
        row = (
            str(uuid.uuid4()),
            campaign_id,
            recipient.lower(),
            subject,
            from_email,
            envelope,
            message_id,
            status,
            error or None,
            profile,
            datetime.now(timezone.utc).isoformat(),
        )
        with self._lock:
            self._conn.execute(
                """INSERT INTO sends
                   (send_id, campaign_id, recipient, subject, from_email,
                    envelope, message_id, status, error, profile, sent_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                row,
            )
            self._conn.commit()

    def query(self, sql: str, params: tuple = ()) -> list[dict]:
        """
        Run any SELECT query against the sends table.

        Example:
            db.query("SELECT * FROM sends WHERE status=?", ("failed",))
            db.query("SELECT recipient FROM sends WHERE campaign_id=? AND status='sent'",
                     ("camp_20240514",))
        """
        with self._lock:
            cur = self._conn.execute(sql, params)
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]

    def campaign_summary(self, campaign_id: str) -> dict:
        """Return counts by status for a given campaign."""
        rows = self.query(
            "SELECT status, COUNT(*) as n FROM sends WHERE campaign_id=? GROUP BY status",
            (campaign_id,),
        )
        return {r["status"]: r["n"] for r in rows}

    def close(self):
        self._conn.close()

    def __repr__(self) -> str:
        return f"SendLog(path={self._path!r})"


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 12: BOUNCE MAILBOX PARSER
# ══════════════════════════════════
# WHY: With VERP, bounced emails arrive at unique addresses like:
#        bounces+jabari=gmail.com+aB3x@mail.supportcallsonline.com
#      Your Postfix catches these via a catch-all alias and delivers them
#      to a mailbox. This class connects to that mailbox via IMAP,
#      reads the bounce emails, extracts the original recipient from the
#      VERP address, and immediately adds them to the suppression list.
#
# FLOW:
#   Run this as a cron job or after every campaign:
#     python postfix_mailer.py --process-bounces
#
#   1. Connect to IMAP (your bounce mailbox)
#   2. Fetch all UNSEEN messages in the inbox
#   3. Parse To: / Delivered-To: headers for VERP addresses
#   4. Also scan body for RFC 3464 (Delivery Status Notification) addresses
#   5. Decode VERP → original recipient email
#   6. Add to SuppressionList
#   7. Mark email as SEEN (so we don't reprocess it)
#   8. Log suppressed count to SendLog
#
# POSTFIX CATCH-ALL SETUP (do this once on your VPS):
#   In /etc/postfix/virtual:
#     @mail.supportcallsonline.com  bounces@mail.supportcallsonline.com
#   Then: postmap /etc/postfix/virtual && postfix reload
#   This routes ALL addresses at that domain into one mailbox.
#
# ENV VARS NEEDED:
#   SEKI_BOUNCE_IMAP_HOST=mail.supportcallsonline.com
#   SEKI_BOUNCE_IMAP_USER=bounces@mail.supportcallsonline.com
#   SEKI_BOUNCE_IMAP_PASS=your-mailbox-password
#   SEKI_BOUNCE_IMAP_PORT=993   (SSL, default)
#   SEKI_BOUNCE_IMAP_FOLDER=INBOX
# ══════════════════════════════════════════════════════════════════════════════

class BounceParser:
    """
    IMAP-based bounce processor that auto-suppresses bounced recipients.

    Reads unprocessed bounce emails → decodes VERP addresses →
    feeds discovered addresses into the SuppressionList.
    """

    def __init__(
        self,
        imap_host: str,
        imap_user: str,
        imap_pass: str,
        imap_port: int = 993,
        imap_folder: str = "INBOX",
        suppression: Optional[SuppressionList] = None,
        send_log: Optional["SendLog"] = None,
        verp_secret: str = "",
    ):
        self.imap_host = imap_host
        self.imap_user = imap_user
        self.imap_pass = imap_pass
        self.imap_port = imap_port
        self.imap_folder = imap_folder
        self.suppression = suppression or SuppressionList()
        self.send_log = send_log
        self.verp_secret = verp_secret

    @classmethod
    def from_env(
        cls,
        prefix: str = "SEKI",
        suppression: Optional[SuppressionList] = None,
        send_log: Optional["SendLog"] = None,
    ) -> "BounceParser":
        """Load bounce mailbox config from environment variables."""
        host = os.getenv(f"{prefix}_BOUNCE_IMAP_HOST", "")
        user = os.getenv(f"{prefix}_BOUNCE_IMAP_USER", "")
        pwd  = os.getenv(f"{prefix}_BOUNCE_IMAP_PASS", "")
        if not all([host, user, pwd]):
            raise EnvironmentError(
                f"Missing bounce IMAP config. Set:\n"
                f"  {prefix}_BOUNCE_IMAP_HOST\n"
                f"  {prefix}_BOUNCE_IMAP_USER\n"
                f"  {prefix}_BOUNCE_IMAP_PASS"
            )
        return cls(
            imap_host=host,
            imap_user=user,
            imap_pass=pwd,
            imap_port=int(os.getenv(f"{prefix}_BOUNCE_IMAP_PORT", "993")),
            imap_folder=os.getenv(f"{prefix}_BOUNCE_IMAP_FOLDER", "INBOX"),
            suppression=suppression,
            send_log=send_log,
            verp_secret=os.getenv(f"{prefix}_VERP_SECRET", ""),
        )

    def _extract_verp_recipients(self, raw_email: bytes) -> list[str]:
        """
        Parse a raw bounce email and extract all VERP-encoded recipients.

        Checks (in order):
          1. To: header         — some MTAs put VERP address here
          2. Delivered-To:      — Postfix adds this for catch-all deliveries
          3. X-Original-To:     — Postfix: original envelope recipient
          4. Return-Path:       — of the original message (if DSN)
          5. DSN Final-Recipient: — RFC 3464 delivery status notifications
          6. Entire raw body    — regex sweep as final fallback
        """
        msg = email_lib.message_from_bytes(raw_email)
        candidates: list[str] = []

        # Headers that may carry VERP address
        for header in ("To", "Delivered-To", "X-Original-To", "Return-Path"):
            val = msg.get(header, "")
            if val:
                candidates.append(val.strip("<>").strip())

        # RFC 3464 Delivery Status Notification body parts
        for part in msg.walk():
            ct = part.get_content_type()
            if ct == "message/delivery-status":
                dsn_text = part.get_payload(decode=True) or b""
                if isinstance(dsn_text, bytes):
                    dsn_text = dsn_text.decode(errors="replace")
                # Extract Final-Recipient field
                for line in dsn_text.splitlines():
                    if line.lower().startswith("final-recipient:"):
                        addr = line.split(";", 1)[-1].strip()
                        candidates.append(addr)

        # Fallback: regex scan full raw email for VERP pattern
        raw_str = raw_email.decode(errors="replace")
        verp_matches = re.findall(r"bounces\+[^\s<>\"']+", raw_str)
        candidates.extend(verp_matches)

        # Decode each candidate through VERP parser
        results = []
        for candidate in candidates:
            decoded = parse_verp_sender(candidate)
            if decoded:
                results.append(decoded)

        return list(set(results))  # deduplicate

    def process(self, max_messages: int = 500) -> int:
        """
        Connect to IMAP, process unseen bounce emails, return count suppressed.

        Args:
            max_messages: Safety cap — process at most this many per run.

        Returns:
            Number of addresses newly added to suppression list.
        """
        log.info(
            f"BounceParser: connecting to {self.imap_host}:{self.imap_port} "
            f"as {self.imap_user}"
        )

        suppressed_count = 0

        try:
            # Use SSL for port 993, plain IMAP for port 143
            if self.imap_port == 993:
                conn = imaplib.IMAP4_SSL(self.imap_host, self.imap_port)
            else:
                conn = imaplib.IMAP4(self.imap_host, self.imap_port)
            conn.login(self.imap_user, self.imap_pass)
            conn.select(self.imap_folder)

            # Search for UNSEEN messages only (won't reprocess already-seen)
            _, data = conn.search(None, "UNSEEN")
            message_ids = data[0].split()

            if not message_ids:
                log.info("BounceParser: no new bounce messages found")
                conn.logout()
                return 0

            # Cap to max_messages for safety
            to_process = message_ids[:max_messages]
            log.info(
                f"BounceParser: found {len(message_ids)} unseen messages, "
                f"processing {len(to_process)}"
            )

            for uid in to_process:
                try:
                    _, msg_data = conn.fetch(uid, "(RFC822)")
                    raw = msg_data[0][1]

                    recipients = self._extract_verp_recipients(raw)

                    for addr in recipients:
                        was_new = not self.suppression.is_suppressed(addr)
                        self.suppression.add(addr)
                        if was_new:
                            suppressed_count += 1
                            log.info(f"BounceParser: suppressed {addr}")

                            # Log bounce event to SQLite if send_log is available
                            if self.send_log:
                                self.send_log.record(
                                    campaign_id="bounce_processing",
                                    recipient=addr,
                                    status="skipped",
                                    error="bounce detected via IMAP",
                                    profile="bounce_parser",
                                )

                    # Mark as SEEN so we don't process again
                    conn.store(uid, "+FLAGS", "\\Seen")

                except Exception as e:
                    log.warning(f"BounceParser: error processing message {uid}: {e}")
                    continue

            conn.logout()

        except imaplib.IMAP4.error as e:
            log.error(f"BounceParser: IMAP error — {e}")
        except (OSError, ConnectionRefusedError) as e:
            log.error(f"BounceParser: connection failed — {e}")

        log.info(
            f"BounceParser: done. {suppressed_count} new addresses suppressed."
        )
        return suppressed_count


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 13: SENTINEL PANEL INTEGRATION
# ════════════════════════════════════════
# WHY: After a campaign finishes, you want to see its stats in your Sentinel
#       panel without logging into the VPS or querying SQLite manually.
#
# HOW IT WORKS:
#   1. Seki finishes send_bulk() → has a Stats object with counts
#   2. SentinelReporter.report() serialises those stats to JSON
#   3. It POSTs the JSON to http://localhost:5055/seki/campaign
#      (same machine → no network hop, instant)
#   4. Your Sentinel webhook stores the payload in a SQLite table
#   5. The Sentinel panel reads that table and shows a Seki Campaigns section
#
# PAYLOAD SHAPE (what Seki sends to Sentinel):
# ─────────────────────────────────────────────────────────────────────────────
#  {
#    "campaign_id":  "supportcallsonline_20240514_143000",
#    "profile":      "supportcallsonline",
#    "subject":      "Hello {{first_name}}",
#    "sent":         842,
#    "failed":       3,
#    "skipped":      12,
#    "dry_run":      0,
#    "retried":      1,
#    "total":        857,
#    "started_at":   "2024-05-14T14:30:00+00:00",
#    "finished_at":  "2024-05-14T14:47:23+00:00",
#    "duration_sec": 1043,
#    "dry_run_mode": false
#  }
#
# SECURITY: The POST includes an X-Seki-Token header. Sentinel webhook checks
#            this token before accepting the payload. Set it via:
#            SEKI_SENTINEL_TOKEN in .env (must match SENTINEL_SEKI_TOKEN on VPS)
#
# FAILURE IS SILENT: If Sentinel is down or unreachable, Seki logs a warning
#                     but does NOT fail the campaign. Sending always wins.
# ══════════════════════════════════════════════════════════════════════════════

class SentinelReporter:
    """
    Posts campaign completion data to Sentinel webhook endpoint.

    Usage (automatic via SekiMailer when sentinel_url is set):
        reporter = SentinelReporter(
            sentinel_url="http://localhost:5055",
            token="your-shared-secret",
        )
        reporter.report(campaign_id="...", profile="...", stats=stats_obj, ...)

    Or load from env:
        reporter = SentinelReporter.from_env()
    """

    ENDPOINT = "/seki/campaign"   # Route we're adding to Sentinel webhook.py

    def __init__(self, sentinel_url: str, token: str = "", timeout: int = 10):
        """
        Args:
            sentinel_url : Base URL of Sentinel webhook  (e.g. http://localhost:5055)
            token        : Shared secret — must match SENTINEL_SEKI_TOKEN on VPS
            timeout      : HTTP request timeout in seconds
        """
        self.url     = sentinel_url.rstrip("/") + self.ENDPOINT
        self.token   = token
        self.timeout = timeout

    @classmethod
    def from_env(cls, prefix: str = "SEKI") -> Optional["SentinelReporter"]:
        """
        Create a SentinelReporter from environment variables.
        Returns None if SENTINEL_URL is not set (so it's fully optional).

        Env vars:
            SEKI_SENTINEL_URL    e.g. http://localhost:5055
            SEKI_SENTINEL_TOKEN  shared secret (any random string)
        """
        url = os.getenv(f"{prefix}_SENTINEL_URL", "")
        if not url:
            return None   # Sentinel integration is disabled — that's fine
        token = os.getenv(f"{prefix}_SENTINEL_TOKEN", "")
        return cls(sentinel_url=url, token=token)

    def report(
        self,
        campaign_id: str,
        profile: str,
        stats: Stats,
        subject: str = "",
        started_at: Optional[str] = None,
        finished_at: Optional[str] = None,
        dry_run_mode: bool = False,
    ) -> bool:
        """
        POST campaign summary to Sentinel panel.

        Args:
            campaign_id  : Unique ID for this campaign (also used as DB key)
            profile      : Profile name (e.g. 'supportcallsonline')
            stats        : Stats object from SekiMailer.send_bulk()
            subject      : Subject line used in the campaign
            started_at   : ISO timestamp when campaign began
            finished_at  : ISO timestamp when campaign ended (defaults to now)
            dry_run_mode : Whether campaign was a dry run

        Returns:
            True if Sentinel accepted the payload, False on any error.
        """
        now = datetime.now(timezone.utc).isoformat()
        finished_at = finished_at or now

        # Calculate duration in seconds from ISO timestamps
        duration_sec = 0
        if started_at:
            try:
                t_start = datetime.fromisoformat(started_at)
                t_end   = datetime.fromisoformat(finished_at)
                duration_sec = int((t_end - t_start).total_seconds())
            except ValueError:
                pass

        total = stats.sent + stats.failed + stats.skipped + stats.dry_run

        payload = {
            "campaign_id":  campaign_id,
            "profile":      profile,
            "subject":      subject,
            "sent":         stats.sent,
            "failed":       stats.failed,
            "skipped":      stats.skipped,
            "dry_run":      stats.dry_run,
            "retried":      stats.retried,
            "total":        total,
            "started_at":   started_at or now,
            "finished_at":  finished_at,
            "duration_sec": duration_sec,
            "dry_run_mode": dry_run_mode,
        }

        body = json.dumps(payload).encode("utf-8")

        req = urllib.request.Request(
            url=self.url,
            data=body,
            method="POST",
            headers={
                "Content-Type":  "application/json",
                "X-Seki-Token":  self.token,
                "User-Agent":    "SekiMailer/2.2",
            },
        )

        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                status = resp.status
                if status == 200:
                    log.info(
                        f"📡 Sentinel: campaign '{campaign_id}' reported successfully "
                        f"({stats.sent} sent, {stats.failed} failed)"
                    )
                    return True
                else:
                    log.warning(f"📡 Sentinel: unexpected response {status} for campaign '{campaign_id}'")
                    return False

        except urllib.error.HTTPError as e:
            log.warning(f"📡 Sentinel: HTTP {e.code} when reporting campaign '{campaign_id}' — {e.reason}")
        except urllib.error.URLError as e:
            log.warning(f"📡 Sentinel: unreachable ({e.reason}) — campaign stats not sent (non-fatal)")
        except Exception as e:
            log.warning(f"📡 Sentinel: unexpected error — {e} (non-fatal)")

        return False


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 14: TELEGRAM ALERTS
# ════════════════════════════
# WHY: After a campaign you want to know the result instantly on your phone
#       without SSHing into the VPS or opening the Sentinel panel.
#
# HOW IT WORKS:
#   After send_bulk() finishes, TelegramNotifier.notify() sends a message
#   to your bot via the Telegram Bot API (HTTPS POST to api.telegram.org).
#   Uses urllib (stdlib) — zero new dependencies.
#
# MESSAGE FORMAT:
#   🚀 Campaign complete
#   ──────────────────
#   Profile : supportcallsonline
#   Subject : Hello {{first_name}}
#   ──────────────────
#   ✅ Sent     : 842
#   ❌ Failed   : 3
#   ⏭ Skipped  : 12
#   🔁 Retried  : 1
#   📬 Total    : 857
#   ⏱ Duration : 17m 23s
#   ──────────────────
#   🔍 Dry-run mode
#   (only shown if dry_run=True)
#
# ENV VARS (already in your .env from Sentinel):
#   TELEGRAM_BOT_TOKEN   your bot token
#   TELEGRAM_CHAT_ID     your personal chat ID
#
# FAILURE IS SILENT: If Telegram is unreachable, Seki logs a warning
#                     and continues — never blocks or fails a campaign.
# ══════════════════════════════════════════════════════════════════════════════

class TelegramNotifier:
    """
    Sends campaign completion alerts to a Telegram bot.

    Usage:
        notifier = TelegramNotifier(bot_token="...", chat_id="...")
        notifier.notify(campaign_id="...", profile="...", stats=stats, ...)

    Or load from env:
        notifier = TelegramNotifier.from_env()   # uses TELEGRAM_* vars
    """

    API_URL = "https://api.telegram.org/bot{token}/sendMessage"

    def __init__(self, bot_token: str, chat_id: str, timeout: int = 10):
        self.bot_token = bot_token
        self.chat_id   = str(chat_id)
        self.timeout   = timeout

    @classmethod
    def from_env(cls, prefix: str = "") -> Optional["TelegramNotifier"]:
        """
        Load from environment variables.
        Uses TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID (no prefix needed —
        these are shared across Sentinel and Seki on the same VPS).
        Returns None if either var is missing.
        """
        token   = os.getenv("TELEGRAM_BOT_TOKEN", "")
        chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
        if not token or not chat_id:
            return None
        return cls(bot_token=token, chat_id=chat_id)

    @staticmethod
    def _fmt_duration(seconds: int) -> str:
        """Convert seconds to a human-readable duration string."""
        if seconds < 60:
            return f"{seconds}s"
        m, s = divmod(seconds, 60)
        if m < 60:
            return f"{m}m {s}s"
        h, m = divmod(m, 60)
        return f"{h}h {m}m"

    def notify(
        self,
        campaign_id: str,
        profile: str,
        stats: Stats,
        subject: str = "",
        duration_sec: int = 0,
        dry_run_mode: bool = False,
    ) -> bool:
        """
        Send a campaign summary message to Telegram.

        Args:
            campaign_id  : Campaign identifier
            profile      : Profile name (e.g. 'supportcallsonline')
            stats        : Stats object from SekiMailer.send_bulk()
            subject      : Subject line used in the campaign
            duration_sec : How long the campaign took in seconds
            dry_run_mode : Whether this was a dry run

        Returns:
            True if message delivered, False on any error (silent failure).
        """
        total = stats.sent + stats.failed + stats.skipped + stats.dry_run

        lines = [
            "🚀 <b>Campaign complete</b>",
            "──────────────────",
            f"<b>Profile</b> : <code>{profile}</code>",
        ]
        if subject:
            subj = subject[:60] + "…" if len(subject) > 60 else subject
            lines.append(f"<b>Subject</b> : {subj}")
        lines += [
            "──────────────────",
            f"✅ Sent     : <b>{stats.sent}</b>",
            f"❌ Failed   : <b>{stats.failed}</b>",
            f"⏭ Skipped  : <b>{stats.skipped}</b>",
            f"🔁 Retried  : <b>{stats.retried}</b>",
            f"📬 Total    : <b>{total}</b>",
        ]
        if duration_sec:
            lines.append(f"⏱ Duration : <b>{self._fmt_duration(duration_sec)}</b>")
        if dry_run_mode:
            lines.append("──────────────────")
            lines.append("🔍 <i>Dry-run mode — nothing actually sent</i>")

        text = "\n".join(lines)

        payload = json.dumps({
            "chat_id":    self.chat_id,
            "text":       text,
            "parse_mode": "HTML",
        }).encode("utf-8")

        url = self.API_URL.format(token=self.bot_token)
        req = urllib.request.Request(
            url=url,
            data=payload,
            method="POST",
            headers={"Content-Type": "application/json"},
        )

        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                if resp.status == 200:
                    log.info(f"📱 Telegram: campaign alert sent for '{campaign_id}'")
                    return True
                log.warning(f"📱 Telegram: unexpected status {resp.status}")
                return False
        except urllib.error.HTTPError as e:
            log.warning(f"📱 Telegram: HTTP {e.code} — {e.reason} (non-fatal)")
        except urllib.error.URLError as e:
            log.warning(f"📱 Telegram: unreachable — {e.reason} (non-fatal)")
        except Exception as e:
            log.warning(f"📱 Telegram: error — {e} (non-fatal)")
        return False


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 10: CLI ENTRY POINT
# ════════════════════════════
# Run from terminal:
#   python postfix_mailer.py --csv recipients.csv \
#     --subject "Hello {{first_name}}" --html template.html --dry-run
# ══════════════════════════════════════════════════════════════════════════════

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Seki Mailer — Direct SMTP via Postfix",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Dry-run to preview without sending
  python postfix_mailer.py --csv contacts.csv --subject "Hi {{first_name}}" --html email.html --dry-run

  # Live send with 10 workers, 0.2s rate limit
  python postfix_mailer.py --csv contacts.csv --subject "Update" --html email.html --workers 10 --rate 0.2

  # Use a different profile (CRYPTO_ env vars)
  python postfix_mailer.py --csv contacts.csv --subject "Alert" --html email.html --profile CRYPTO
        """
    )
    p.add_argument("--csv",      required=False,       help="Path to CSV recipient file (must have 'email' column)")
    p.add_argument("--subject",  default=None,         help="Email subject line (supports {{variables}})")
    p.add_argument("--html",     default=None,         help="Path to HTML template file")
    p.add_argument("--plain",    default=None,         help="Path to plain-text template (auto-generated if omitted)")
    p.add_argument("--profile",  default="SEKI",       help="Env variable prefix for sender profile (default: SEKI)")
    p.add_argument("--workers",  type=int, default=5,  help="Concurrent sending threads (default: 5)")
    p.add_argument("--rate",     type=float, default=0.1, help="Rate limit: seconds between sends per thread (default: 0.1)")
    p.add_argument("--retries",  type=int, default=3,  help="Max retries on transient SMTP errors (default: 3)")
    p.add_argument("--smtp-host", default="127.0.0.1", help="SMTP host (default: 127.0.0.1)")
    p.add_argument("--smtp-port", type=int, default=25, help="SMTP port (default: 25)")
    p.add_argument("--suppress",  default="suppressed.txt", help="Path to suppression list file")
    p.add_argument("--db",        default="seki_sends.db",  help="Path to SQLite send log database (default: seki_sends.db)")
    p.add_argument("--campaign",  default=None,        help="Campaign ID tag for this send batch (auto-generated if omitted)")
    p.add_argument("--sentinel-url",   default=None,   help="Sentinel webhook base URL (e.g. http://localhost:5055)")
    p.add_argument("--sentinel-token", default=None,   help="Shared secret token for Sentinel integration")
    p.add_argument("--dry-run",  action="store_true",  help="Build and log messages but do NOT deliver")
    p.add_argument("--process-bounces", action="store_true",
                   help="Connect to bounce mailbox via IMAP and auto-suppress bounced addresses")
    return p.parse_args()


def main():
    args = _parse_args()

    # ── Shared resources ──────────────────────────────────────────────────────
    suppression = SuppressionList(args.suppress)
    send_log    = SendLog(args.db)

    # ── Optional Sentinel reporter ────────────────────────────────────────────
    # Priority: CLI flags > env vars > disabled
    sentinel_url   = args.sentinel_url   or os.getenv(f"{args.profile}_SENTINEL_URL", "")
    sentinel_token = args.sentinel_token or os.getenv(f"{args.profile}_SENTINEL_TOKEN", "")
    sentinel = SentinelReporter(sentinel_url, sentinel_token) if sentinel_url else None
    if sentinel:
        log.info(f"Sentinel integration: enabled → {sentinel.url}")
    else:
        log.info("Sentinel integration: disabled (set SEKI_SENTINEL_URL to enable)")

    # ── Optional Telegram notifier ────────────────────────────────────────────
    telegram = TelegramNotifier.from_env()
    if telegram:
        log.info("Telegram alerts: enabled")
    else:
        log.info("Telegram alerts: disabled (set TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID to enable)")

    # ── Mode: process bounces only ────────────────────────────────────────────
    if args.process_bounces:
        log.info("Running in bounce-processing mode...")
        parser = BounceParser.from_env(
            prefix=args.profile,
            suppression=suppression,
            send_log=send_log,
        )
        count = parser.process()
        log.info(f"Bounce processing complete. {count} addresses suppressed.")
        send_log.close()
        return

    # ── Mode: send campaign ───────────────────────────────────────────────────
    if not args.csv or not args.subject or not args.html:
        print("Error: --csv, --subject, and --html are required for sending.\n"
              "       Use --process-bounces to run bounce processing instead.")
        raise SystemExit(1)

    # Load profile
    profile = SenderProfile.from_env(prefix=args.profile)

    # Load templates
    html_body  = Path(args.html).read_text(encoding="utf-8")
    plain_body = Path(args.plain).read_text(encoding="utf-8") if args.plain else None

    # Load recipients
    recipients = SekiMailer.load_csv(args.csv)

    # Run
    mailer = SekiMailer(
        profile=profile,
        smtp_host=args.smtp_host,
        smtp_port=args.smtp_port,
        dry_run=args.dry_run,
        max_workers=args.workers,
        rate_limit=args.rate,
        max_retries=args.retries,
        suppression=suppression,
        send_log=send_log,
        campaign_id=args.campaign,
        sentinel=sentinel,
        telegram=telegram,
    )

    mailer.send_bulk(
        recipients=recipients,
        subject=args.subject,
        html_body=html_body,
        plain_body=plain_body,
    )

    # Print DB summary for this campaign
    if mailer.campaign_id:
        summary = send_log.campaign_summary(mailer.campaign_id)
        log.info(f"DB summary for campaign '{mailer.campaign_id}': {summary}")

    send_log.close()


if __name__ == "__main__":
    main()
