r"""
src/gmail_client.py — Gmail API wrapper for Phase 7 (watcher) and Phase 10 (digest).

WHY THIS WRAPPER EXISTS
-----------------------
The raw `googleapiclient.discovery.build('gmail', 'v1', ...)` object is
verbose and stateful (auth handling, message-body encoding, base64-url
quirks). Wrapping it in a few task-specific functions keeps the watcher
and digest modules readable.

AUTHENTICATION (ONE-TIME OAUTH SETUP)
-------------------------------------
1. Go to https://console.cloud.google.com/
2. Create a new project (e.g. "job-hunt-agent")
3. APIs & Services → Library → enable **Gmail API**
4. APIs & Services → OAuth consent screen
   - User type: External
   - App name: "Job Hunt Agent" (or anything)
   - User support email: codewithsourabhjha@gmail.com
   - Developer contact: codewithsourabhjha@gmail.com
   - Scopes: add `.../auth/gmail.modify`
   - Test users: add codewithsourabhjha@gmail.com (stays in test mode forever — fine)
5. APIs & Services → Credentials → Create Credentials → OAuth client ID
   - Application type: **Desktop app**
   - Name: "job-hunt-cli" (or anything)
6. Download the JSON file → save it to project root as `credentials.json`
7. First run of `python -m src.gmail_watcher` opens a browser for consent.
   After approval, a `token.json` file is cached locally. Both are gitignored.

WHY DESKTOP APP TYPE
--------------------
Desktop apps use the "installed app" OAuth flow which spins up a local
loopback server to receive the redirect. No need to host a callback URL.
Works on a laptop without any web server. The free tier of GCP allows
this indefinitely as long as you stay in "Testing" mode with yourself
listed as a Test User.
"""

from __future__ import annotations

import base64
import logging
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any, Optional

from config import GMAIL_CREDENTIALS_PATH, GMAIL_SCOPES, GMAIL_TOKEN_PATH

log = logging.getLogger(__name__)


# =============================================================================
# Authentication
# =============================================================================
def _load_credentials():
    """
    Return google.oauth2.credentials.Credentials, either from cached
    token.json or by running the OAuth install flow (which opens a browser).

    Raises FileNotFoundError with a clear, actionable message when
    credentials.json is missing — that's the most common first-run failure.
    """
    # Lazy-import the heavy google-auth libs so this module is cheap to
    # import even when Gmail isn't being used (e.g. in tests).
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow

    creds: Optional[Credentials] = None

    if GMAIL_TOKEN_PATH.exists():
        try:
            creds = Credentials.from_authorized_user_file(
                str(GMAIL_TOKEN_PATH), GMAIL_SCOPES,
            )
        except Exception as exc:  # noqa: BLE001 — token file might be corrupt
            log.warning("gmail: token.json corrupt (%s) — re-running OAuth flow", exc)
            creds = None

    # Refresh expired credentials silently if we have a refresh token.
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.request(Request())
        except Exception:
            try:
                creds.refresh(Request())
            except Exception as exc:  # noqa: BLE001
                log.warning("gmail: token refresh failed (%s) — re-running OAuth flow", exc)
                creds = None

    if not creds or not creds.valid:
        if not GMAIL_CREDENTIALS_PATH.exists():
            raise FileNotFoundError(
                f"\nGmail credentials.json not found at {GMAIL_CREDENTIALS_PATH}.\n"
                f"\nSet it up in ~5 minutes:\n"
                f"  1. https://console.cloud.google.com/ → create project 'job-hunt-agent'\n"
                f"  2. Enable 'Gmail API' (APIs & Services → Library)\n"
                f"  3. Configure OAuth consent (External, add yourself as Test User,\n"
                f"     add scope: .../auth/gmail.modify)\n"
                f"  4. Credentials → Create OAuth client ID → Desktop app\n"
                f"  5. Download the JSON, save as: {GMAIL_CREDENTIALS_PATH}\n"
                f"  6. Re-run this command — a browser window will pop up for consent\n"
            )
        log.info("gmail: starting OAuth install flow (browser will open)")
        flow = InstalledAppFlow.from_client_secrets_file(
            str(GMAIL_CREDENTIALS_PATH), GMAIL_SCOPES,
        )
        creds = flow.run_local_server(port=0)  # port=0 → OS picks any free port
        # Cache for next time.
        GMAIL_TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")
        log.info("gmail: token cached to %s", GMAIL_TOKEN_PATH)

    return creds


def get_service():
    """
    Return an authenticated Gmail API service object. Cached per-process
    via module-level `_SERVICE` so we don't re-auth on every call.
    """
    global _SERVICE
    if _SERVICE is None:
        from googleapiclient.discovery import build
        creds = _load_credentials()
        _SERVICE = build("gmail", "v1", credentials=creds, cache_discovery=False)
    return _SERVICE


_SERVICE = None   # populated lazily by get_service()


# =============================================================================
# Reading mail
# =============================================================================
def list_messages(query: str, max_results: int = 100) -> list[dict]:
    """
    Run a Gmail search and return raw message IDs + thread IDs.
    `query` uses Gmail's standard search syntax (e.g. 'from:greenhouse.io after:2026/05/01').

    Returns a list of {'id': ..., 'threadId': ...} dicts; call get_message() to
    fetch the full payload.
    """
    service = get_service()
    out: list[dict] = []
    page_token: Optional[str] = None
    while True:
        resp = service.users().messages().list(
            userId="me",
            q=query,
            maxResults=min(max_results - len(out), 100),
            pageToken=page_token,
        ).execute()
        out.extend(resp.get("messages") or [])
        page_token = resp.get("nextPageToken")
        if not page_token or len(out) >= max_results:
            break
    return out[:max_results]


def get_message(message_id: str) -> dict:
    """Fetch a full message (headers + body). Format='full' gives parsed parts."""
    service = get_service()
    return service.users().messages().get(
        userId="me", id=message_id, format="full",
    ).execute()


def extract_message_fields(msg: dict) -> dict:
    """
    Pull the bits Phase 7 actually needs out of Gmail's deeply nested format.
    Returns {subject, from, from_email, date, snippet, body_text, labels}.
    """
    headers = {h["name"].lower(): h["value"] for h in msg.get("payload", {}).get("headers", [])}
    subject = headers.get("subject", "")
    from_raw = headers.get("from", "")
    # Pull the email out of "Display Name <email@example.com>"
    from_email = from_raw
    if "<" in from_raw and ">" in from_raw:
        from_email = from_raw.split("<", 1)[1].split(">", 1)[0].strip()

    body_text = _extract_text_body(msg.get("payload", {}))

    return {
        "id":         msg.get("id", ""),
        "thread_id":  msg.get("threadId", ""),
        "subject":    subject,
        "from":       from_raw,
        "from_email": from_email,
        "date":       headers.get("date", ""),
        "snippet":    msg.get("snippet", ""),
        "body_text":  body_text,
        "labels":     msg.get("labelIds", []),
        # internalDate is a Gmail-set epoch ms (reliable; the 'Date' header
        # can be missing or wrong from misconfigured ATS systems).
        "internal_date_ms": int(msg.get("internalDate", 0) or 0),
    }


def _extract_text_body(payload: dict) -> str:
    """
    Walk the MIME tree to find the plain-text part. Falls back to
    stripping HTML if the message is HTML-only.
    """
    if not payload:
        return ""

    # Single-part: just decode it.
    if "body" in payload and payload.get("body", {}).get("data"):
        data = payload["body"]["data"]
        decoded = _b64url_decode(data)
        mime = payload.get("mimeType", "")
        if mime == "text/plain":
            return decoded
        if mime == "text/html":
            return _strip_html(decoded)
        return decoded  # unknown mime → return raw bytes-as-text

    # Multi-part: prefer text/plain, fall back to text/html.
    parts = payload.get("parts") or []
    text_plain = ""
    text_html = ""
    for part in parts:
        mime = part.get("mimeType", "")
        if mime == "text/plain" and part.get("body", {}).get("data"):
            text_plain += _b64url_decode(part["body"]["data"])
        elif mime == "text/html" and part.get("body", {}).get("data"):
            text_html += _b64url_decode(part["body"]["data"])
        elif mime.startswith("multipart/"):
            # Recursive — multipart/alternative or related.
            text_plain += _extract_text_body(part)

    if text_plain:
        return text_plain
    if text_html:
        return _strip_html(text_html)
    return ""


def _b64url_decode(data: str) -> str:
    """Gmail uses URL-safe base64 without padding; we add the padding back."""
    try:
        padding = "=" * (-len(data) % 4)
        return base64.urlsafe_b64decode(data + padding).decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        return ""


def _strip_html(html: str) -> str:
    """Tiny HTML stripper — kept here so this module has zero extra deps."""
    import re
    html = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.IGNORECASE | re.DOTALL)
    html = re.sub(r"<[^>]+>", " ", html)
    html = re.sub(r"\s+", " ", html).strip()
    replacements = {"&amp;": "&", "&lt;": "<", "&gt;": ">", "&quot;": '"',
                    "&#39;": "'", "&nbsp;": " "}
    for k, v in replacements.items():
        html = html.replace(k, v)
    return html


# =============================================================================
# Writing mail
# =============================================================================
def _build_mime(to: str, subject: str, body: str, from_addr: Optional[str] = None) -> dict:
    """Encode a plain-text email into Gmail's raw-base64-url format."""
    msg = MIMEText(body, _charset="utf-8")
    msg["To"] = to
    msg["Subject"] = subject
    if from_addr:
        msg["From"] = from_addr
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")
    return {"raw": raw}


def send_email(to: str, subject: str, body: str) -> str:
    """Send a plain-text email immediately. Returns Gmail message ID."""
    service = get_service()
    resp = service.users().messages().send(
        userId="me", body=_build_mime(to, subject, body),
    ).execute()
    log.info("gmail: sent message id=%s subject=%r to=%s", resp.get("id"), subject, to)
    return resp.get("id", "")


def create_draft(to: str, subject: str, body: str) -> str:
    """
    Save a draft (does NOT send). Used by Phase 10 for auto-drafted
    follow-up emails for ghosted apps — Sourabh reviews + sends manually.
    """
    service = get_service()
    resp = service.users().drafts().create(
        userId="me", body={"message": _build_mime(to, subject, body)},
    ).execute()
    log.info("gmail: created draft id=%s subject=%r to=%s", resp.get("id"), subject, to)
    return resp.get("id", "")
