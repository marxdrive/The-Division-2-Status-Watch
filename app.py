from __future__ import annotations

import base64
import json
import logging
import os
import re
import smtplib
import threading
from datetime import datetime, timezone
from email.message import EmailMessage
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO").upper(),
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("division2-watch")

PAGE_URL = "https://www.ubisoft.com/en-us/game/the-division/the-division-2/status"
WIDGET_URL = "https://ubistatic-a.akamaihd.net/0115/tctd2/status.html"
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
SETTINGS_PATH = DATA_DIR / "settings.json"
STATE_PATH = DATA_DIR / "state.json"
BACKGROUND_PATH = Path(__file__).with_name("background.png")
WEB_USERNAME = os.environ.get("WEB_USERNAME", "admin")
WEB_PASSWORD = os.environ["WEB_PASSWORD"]
PORT = int(os.environ.get("PORT", "8080"))
STATES = {
    "operational": ("operational", "online", "available", "all systems normal"),
    "maintenance": ("maintenance", "maintaining"),
    "problems": ("problems", "problem", "degraded", "limited service", "partial outage"),
    "outage": ("outage", "outages", "offline", "down", "unavailable"),
}
ALLOWED_INTERVALS = (3, 5, 10, 15, 20, 25, 30, 60)
ALERT_STATES = ("operational", "maintenance", "problems", "outage")
STATUS_LABELS = {
    "operational": "Online (Operational)",
    "maintenance": "Maintenance",
    "problems": "Problems",
    "outage": "Outage",
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def read_json(path: Path, default: dict) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default.copy()


def write_json(path: Path, value: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2), encoding="utf-8")
    temporary.replace(path)


def default_settings() -> dict:
    return {
        "interval_minutes": 5, "notify_statuses": ["operational"],
        "method": "none", "email_to": "",
        "smtp_host": "", "smtp_port": 587, "smtp_security": "starttls",
        "smtp_username": "", "smtp_password": "", "email_from": "",
        "phone_to": "", "twilio_account_sid": "", "twilio_auth_token": "",
        "twilio_from": "", "gotify_server": "", "gotify_app_token": "",
        "gotify_priority": 5,
    }


def get_settings() -> dict:
    result = default_settings()
    result.update(read_json(SETTINGS_PATH, {}))
    try:
        interval = int(result["interval_minutes"])
        result["interval_minutes"] = min(ALLOWED_INTERVALS, key=lambda value: abs(value - interval))
    except (TypeError, ValueError):
        result["interval_minutes"] = 5
    selected = result.get("notify_statuses", ["operational"])
    if not isinstance(selected, list):
        selected = ["operational"]
    result["notify_statuses"] = [status for status in ALERT_STATES if status in selected]
    return result


def get_initial_state() -> dict:
    result = {
        "status": "unknown", "last_known_status": "unknown",
        "last_checked": None, "last_change": None,
        "error": "Waiting for the first check.", "notification_error": None,
        "pending_notification": None, "source": WIDGET_URL,
    }
    stored = read_json(STATE_PATH, {})
    result.update(stored)
    if "pending_notification" not in stored and stored.get("pending_recovery"):
        result["pending_notification"] = "operational"
    result.pop("pending_recovery", None)
    return result


def state_from_text(value: str) -> list[str]:
    lower = value.lower()
    return [
        state for state, terms in STATES.items()
        if any(re.search(r"(?<![a-z])" + re.escape(term) + r"(?![a-z])", lower) for term in terms)
    ]


class PageSignals(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.text: list[str] = []
        self.attributes: list[str] = []
        self.explicit: list[str] = []
        self.ignored_tags = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript"):
            self.ignored_tags += 1
            return
        values = {key.lower(): value or "" for key, value in attrs}
        self.attributes.append(" ".join(values.values()))
        for key in ("data-status", "data-state"):
            self.explicit.extend(state_from_text(values.get(key, "")))

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript") and self.ignored_tags:
            self.ignored_tags -= 1

    def handle_data(self, data):
        if not self.ignored_tags:
            self.text.append(data)


def detect_live_status(document: str) -> str | None:
    parser = PageSignals()
    parser.feed(document)
    explicit = list(dict.fromkeys(parser.explicit))
    if len(explicit) == 1:
        return explicit[0]
    if explicit:
        return None

    # The Ubisoft display includes one icon for each state as a legend.
    # A state appearing again in accessible image or title attributes is a
    # live signal; the legend by itself is deliberately treated as unknown.
    attribute_states = state_from_text(" ".join(parser.attributes))
    counts = {state: attribute_states.count(state) for state in STATES}
    extras = {state: max(0, count - 1) for state, count in counts.items()}
    winners = [state for state, count in extras.items() if count == max(extras.values()) and count > 0]
    if len(winners) == 1:
        return winners[0]
    if winners:
        return None

    text_states = state_from_text(" ".join(parser.text))
    distinct = list(dict.fromkeys(text_states))
    if len(distinct) == 1:
        return distinct[0]
    if not distinct:
        return None
    counts = {state: text_states.count(state) for state in distinct}
    top = max(counts.values())
    winners = [state for state, count in counts.items() if count == top]
    return winners[0] if top > 1 and len(winners) == 1 else None


def fetch_page(url: str) -> str:
    request = Request(url, headers={
        "User-Agent": "Division2StatusWatch/1.0 (self-hosted status monitor)",
        "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
        "Cache-Control": "no-cache",
    })
    with urlopen(request, timeout=25) as response:
        return response.read(2_000_000).decode(
            response.headers.get_content_charset() or "utf-8", errors="replace"
        )


def check_ubisoft() -> tuple[str | None, str | None, str]:
    errors = []
    for url in (WIDGET_URL, PAGE_URL):
        try:
            status = detect_live_status(fetch_page(url))
            if status:
                return status, None, url
            errors.append(f"{url}: no unambiguous live status found")
        except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
            errors.append(f"{url}: {exc}")
    return None, "; ".join(errors), WIDGET_URL


def send_email(settings: dict, subject: str, body: str) -> None:
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = settings["email_from"]
    message["To"] = settings["email_to"]
    message.set_content(body)
    security = settings["smtp_security"]
    if security == "ssl":
        with smtplib.SMTP_SSL(settings["smtp_host"], int(settings["smtp_port"]), timeout=20) as smtp:
            if settings["smtp_username"]:
                smtp.login(settings["smtp_username"], settings["smtp_password"])
            smtp.send_message(message)
    else:
        with smtplib.SMTP(settings["smtp_host"], int(settings["smtp_port"]), timeout=20) as smtp:
            smtp.ehlo()
            if security == "starttls":
                smtp.starttls()
                smtp.ehlo()
            if settings["smtp_username"]:
                smtp.login(settings["smtp_username"], settings["smtp_password"])
            smtp.send_message(message)


def send_sms(settings: dict, body: str) -> None:
    sid = settings["twilio_account_sid"]
    url = f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"
    data = urlencode({
        "To": settings["phone_to"], "From": settings["twilio_from"], "Body": body,
    }).encode("utf-8")
    token = base64.b64encode(f"{sid}:{settings['twilio_auth_token']}".encode()).decode("ascii")
    request = Request(url, data=data, method="POST", headers={
        "Authorization": f"Basic {token}",
        "Content-Type": "application/x-www-form-urlencoded",
        "User-Agent": "Division2StatusWatch/1.0",
    })
    with urlopen(request, timeout=20) as response:
        response.read(4096)


def send_gotify(
    settings: dict,
    body: str,
    title: str = "The Division 2 is back online",
) -> None:
    server = settings["gotify_server"].strip().rstrip("/")
    if server.endswith("/message"):
        server = server[:-8]
    parsed = urlsplit(server)
    if parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.query or parsed.fragment:
        raise ValueError("Enter a Gotify server URL beginning with http:// or https://.")
    if parsed.username or parsed.password:
        raise ValueError("Do not put credentials in the Gotify server URL.")
    payload = json.dumps({
        "title": title,
        "message": body,
        "priority": int(settings["gotify_priority"]),
    }).encode("utf-8")
    token = settings["gotify_app_token"]

    def post(headers: dict) -> None:
        request = Request(
            server + "/message",
            data=payload,
            method="POST",
            headers={
                **headers,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        with urlopen(request, timeout=20) as response:
            if not 200 <= response.status < 300:
                raise RuntimeError(f"Gotify returned HTTP {response.status}.")
            response.read(4096)

    try:
        post({"X-Gotify-Key": token})
    except HTTPError as first_error:
        if first_error.code != 401:
            raise
        first_error.close()
        try:
            # Some reverse proxies or older integrations handle the standard
            # bearer header while dropping Gotify's custom token header.
            post({"Authorization": f"Bearer {token}"})
        except HTTPError as second_error:
            if second_error.code == 401:
                second_error.close()
                raise RuntimeError(
                    "Gotify rejected the token in both documented headers (401). "
                    "Confirm this is an application token for this Gotify server, "
                    "or check whether a reverse proxy is removing authorization headers."
                ) from second_error
            raise


def notify_status(settings: dict, status: str) -> None:
    if status not in STATUS_LABELS:
        raise ValueError("Cannot send a notification for an unknown status.")
    if status == "operational":
        subject = "The Division 2 is back online"
        message = "The Division 2 is back online according to Ubisoft's status page."
    else:
        label = STATUS_LABELS[status]
        subject = f"The Division 2 status: {label}"
        message = f"Ubisoft reports The Division 2 status as {label}."
    body = f"{message}\n\nChecked at {now_iso()} (UTC).\n{PAGE_URL}"
    if settings.get("method") == "email":
        send_email(settings, subject, body)
    elif settings.get("method") == "sms":
        send_sms(settings, f"{message} Ubisoft status: {PAGE_URL}")
    elif settings.get("method") == "gotify":
        send_gotify(settings, body, title=subject)
    else:
        raise ValueError("Choose an alert method in Settings.")


class Monitor:
    def __init__(self):
        self.lock = threading.RLock()
        self.check_lock = threading.Lock()
        self.wake = threading.Event()
        self.state = get_initial_state()
        self.settings = get_settings()
        threading.Thread(target=self._run, daemon=True).start()

    def public_snapshot(self) -> dict:
        with self.lock:
            s = self.settings
            safe = {key: s[key] for key in (
                "interval_minutes", "notify_statuses", "method", "email_to", "smtp_host", "smtp_port",
                "smtp_security", "smtp_username", "email_from", "phone_to",
                "twilio_account_sid", "twilio_from", "gotify_server", "gotify_priority",
            )}
            safe["has_smtp_password"] = bool(s["smtp_password"])
            safe["has_twilio_auth_token"] = bool(s["twilio_auth_token"])
            safe["has_gotify_app_token"] = bool(s["gotify_app_token"])
            return {"state": self.state.copy(), "settings": safe}

    def save_settings(self, incoming: dict) -> dict:
        with self.lock:
            updated = self.settings.copy()
            ordinary = (
                "method", "email_to", "smtp_host", "smtp_security", "smtp_username",
                "email_from", "phone_to", "twilio_account_sid", "twilio_from",
                "gotify_server",
            )
            for key in ordinary:
                if key in incoming:
                    updated[key] = str(incoming[key]).strip()
            if "notify_statuses" in incoming:
                selected = incoming["notify_statuses"]
                if not isinstance(selected, list) or any(status not in ALERT_STATES for status in selected):
                    raise ValueError("Choose valid status alerts.")
                updated["notify_statuses"] = [status for status in ALERT_STATES if status in selected]
            for key in ("smtp_password", "twilio_auth_token", "gotify_app_token"):
                value = str(incoming.get(key, "")).strip()
                if value:
                    updated[key] = value
            try:
                interval = int(incoming.get("interval_minutes", updated["interval_minutes"]))
                updated["smtp_port"] = int(incoming.get("smtp_port", updated["smtp_port"]))
                updated["gotify_priority"] = int(incoming.get("gotify_priority", updated["gotify_priority"]))
            except (TypeError, ValueError):
                raise ValueError("Interval, SMTP port, and Gotify priority must be numbers.")
            if interval not in ALLOWED_INTERVALS:
                raise ValueError("Choose a check interval of 3, 5, 10, 15, 20, 25, 30, or 60 minutes.")
            if not 0 <= updated["gotify_priority"] <= 10:
                raise ValueError("Gotify priority must be from 0 to 10.")
            updated["interval_minutes"] = interval
            if updated["method"] not in ("none", "email", "sms", "gotify"):
                raise ValueError("Choose email, phone message, or Gotify notifications.")
            if updated["smtp_security"] not in ("starttls", "ssl", "none"):
                raise ValueError("Choose a valid email connection security setting.")
            if updated["method"] == "email" and not all(updated[k] for k in ("email_to", "smtp_host", "email_from")):
                raise ValueError("Enter the recipient email, SMTP server, and sender email.")
            if updated["method"] == "sms" and not all(updated[k] for k in ("phone_to", "twilio_account_sid", "twilio_auth_token", "twilio_from")):
                raise ValueError("Enter the phone number and all Twilio sender credentials.")
            if updated["method"] == "gotify" and not all(updated[k] for k in ("gotify_server", "gotify_app_token")):
                raise ValueError("Enter your Gotify server URL and application token.")
            self.settings = updated
            write_json(SETTINGS_PATH, updated)
            self.wake.set()
        return self.public_snapshot()

    def test_gotify(self, incoming: dict) -> None:
        with self.lock:
            settings = self.settings.copy()
        server = str(incoming.get("gotify_server", settings["gotify_server"])).strip()
        token_input = str(incoming.get("gotify_app_token", "")).strip()
        token = token_input or settings["gotify_app_token"]
        try:
            priority = int(incoming.get("gotify_priority", settings["gotify_priority"]))
        except (TypeError, ValueError):
            raise ValueError("Gotify priority must be a number from 0 to 10.")
        if not server:
            raise ValueError("Enter your Gotify server URL.")
        if not token:
            raise ValueError("Enter a Gotify application token.")
        if not 0 <= priority <= 10:
            raise ValueError("Gotify priority must be from 0 to 10.")
        settings.update({
            "gotify_server": server,
            "gotify_app_token": token,
            "gotify_priority": priority,
        })
        send_gotify(
            settings,
            "This is a test push from The Division 2 Status Watch. If you received it, your Gotify setup is working.",
            title="The Division 2 Status Watch test",
        )

    def check_once(self) -> dict:
        if not self.check_lock.acquire(blocking=False):
            return self.public_snapshot()
        try:
            status, error, source = check_ubisoft()
            with self.lock:
                previous = self.state.get("last_known_status", "unknown")
                checked = now_iso()
                self.state.update({
                    "status": status or "unknown", "last_checked": checked,
                    "source": source, "error": error,
                })
                if status:
                    if status != previous:
                        self.state["last_change"] = checked
                        if previous in ALERT_STATES and status in self.settings["notify_statuses"]:
                            self.state["pending_notification"] = status
                        else:
                            self.state["pending_notification"] = None
                        self.state["notification_error"] = None
                    self.state["last_known_status"] = status
                write_json(STATE_PATH, self.state)
                settings = self.settings.copy()
                pending = self.state.get("pending_notification")
                if pending not in settings["notify_statuses"]:
                    self.state["pending_notification"] = None
                    self.state["notification_error"] = None
                    pending = None
                    write_json(STATE_PATH, self.state)
            if status == pending and pending:
                if settings.get("method") not in ("email", "sms", "gotify"):
                    with self.lock:
                        self.state["notification_error"] = f"{STATUS_LABELS[pending]} alert pending. Choose Email, Phone message, or Gotify in settings."
                        write_json(STATE_PATH, self.state)
                else:
                    try:
                        notify_status(settings, pending)
                        log.info("Status notification sent for %s via %s", pending, settings.get("method"))
                        with self.lock:
                            self.state["pending_notification"] = None
                            self.state["notification_error"] = None
                            write_json(STATE_PATH, self.state)
                    except Exception:
                        log.exception("Status notification could not be sent")
                        with self.lock:
                            self.state["notification_error"] = "The status alert could not be sent and will be retried. Check settings and container logs."
                            write_json(STATE_PATH, self.state)
            return self.public_snapshot()
        finally:
            self.check_lock.release()

    def _run(self):
        while True:
            self.check_once()
            with self.lock:
                seconds = self.settings["interval_minutes"] * 60
            self.wake.wait(seconds)
            self.wake.clear()


MONITOR = Monitor()

INDEX_HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="theme-color" content="#10171d"><title>The Division 2 Status Watch</title>
<style>
:root{color-scheme:dark;--bg:#10171d;--panel:#19232b;--line:#2a3944;--muted:#9aadb9;--text:#eef4f6;--green:#54d69a;--amber:#ffc86a;--red:#ff7676;--blue:#83b8ff}
*{box-sizing:border-box}body{margin:0;background-color:var(--bg);background-image:radial-gradient(ellipse at 82% -8%,rgba(40,55,70,.48) 0,transparent 38%),linear-gradient(rgba(9,14,19,.58),rgba(9,14,19,.78)),url('/background.png');background-position:center,center,center;background-size:cover,cover,cover;background-attachment:fixed,fixed,fixed;background-repeat:no-repeat;color:var(--text);font:15px/1.5 system-ui,-apple-system,Segoe UI,sans-serif}
main{width:min(960px,calc(100% - 32px));margin:44px auto 72px}header{display:flex;align-items:center;justify-content:space-between;gap:20px;margin-bottom:24px}
.eyebrow{text-transform:uppercase;letter-spacing:.13em;color:var(--blue);font-size:11px;font-weight:750}h1{margin:4px 0 0;font-size:clamp(25px,4vw,36px);line-height:1.16;letter-spacing:-.035em}
.logo{height:44px;width:44px;display:grid;place-items:center;border:1px solid #405361;border-radius:14px;color:var(--blue);font-weight:850;font-size:18px}
.card{background:linear-gradient(145deg,rgba(28,40,49,.91),rgba(23,33,40,.94));border:1px solid var(--line);border-radius:18px;padding:22px;box-shadow:0 12px 34px #0002;backdrop-filter:blur(3px)}
.status-card{display:grid;grid-template-columns:1fr auto;gap:20px;align-items:center;margin-bottom:18px}.label{color:var(--muted);font-size:12px;font-weight:700;text-transform:uppercase;letter-spacing:.09em}
.status-name{font-size:28px;font-weight:760;letter-spacing:-.03em;margin:2px 0}.status-sub{color:var(--muted);font-size:13px}
.status-pill{border:1px solid var(--line);border-radius:999px;padding:8px 12px;font-size:12px;font-weight:750;text-transform:uppercase;letter-spacing:.08em;white-space:nowrap}
.status-pill.operational,.status-pill.online{color:var(--green);border-color:#39795f}.status-pill.maintenance,.status-pill.problems{color:var(--amber);border-color:#7d673c}.status-pill.outage{color:var(--red);border-color:#7b4242}.status-pill.unknown{color:var(--muted)}
.columns{display:grid;grid-template-columns:1.05fr .95fr;gap:18px;align-items:start}h2{margin:0 0 4px;font-size:19px;letter-spacing:-.02em}.intro{color:var(--muted);margin:0 0 20px;font-size:13px}
.field{margin:0 0 14px}label{display:block;font-size:13px;font-weight:650;margin-bottom:6px}
input,select{width:100%;border:1px solid #3a4a55;border-radius:9px;background:#111a20;color:var(--text);padding:10px 11px;font:inherit;outline:none}
input:focus,select:focus{border-color:var(--blue);box-shadow:0 0 0 3px #83b8ff20}input::placeholder{color:#71838e}
.inline{display:grid;grid-template-columns:1fr 1fr;gap:12px}.radio-row{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin:12px 0 16px}
.radio{border:1px solid #354651;background:#141e24;border-radius:10px;padding:11px 12px;display:flex;gap:9px;align-items:center;cursor:pointer}.radio input{width:auto;accent-color:#83b8ff}
 .status-options{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px}.status-option{display:flex;align-items:center;gap:9px;border:1px solid #354651;background:#141e24;border-radius:10px;padding:10px 12px;font-weight:550;cursor:pointer}.status-option input{width:16px;height:16px;margin:0;accent-color:#83b8ff;flex:none}
.section{border-top:1px solid var(--line);padding-top:16px;margin-top:16px}.section-title{font-size:13px;font-weight:750;margin-bottom:12px}.hint{color:var(--muted);font-size:11px;margin-top:5px}
button{border:0;border-radius:9px;background:#88baff;color:#0c1820;font:750 14px system-ui,sans-serif;padding:11px 16px;cursor:pointer}button:hover{background:#a4cbff}button:disabled{opacity:.55;cursor:wait}
.secondary{background:#263641;color:var(--text);border:1px solid #3b4d58}.secondary:hover{background:#30434f}.actions{display:flex;align-items:center;gap:12px;margin-top:20px}
.message{min-height:20px;font-size:12px;color:var(--muted)}.message.error{color:var(--red)}.message.ok{color:var(--green)}
.info-list{display:grid;gap:12px;margin:16px 0 0}.info-row{display:flex;justify-content:space-between;gap:16px;font-size:13px}.info-row span:first-child{color:var(--muted)}
.notice{border-left:2px solid #60798b;padding:2px 0 2px 12px;margin-top:20px;color:var(--muted);font-size:12px}.source{display:inline-block;margin-top:18px;color:var(--blue);font-size:12px;text-decoration:none}.source:hover{text-decoration:underline}[hidden]{display:none!important}
@media(max-width:740px){main{margin-top:24px}.columns{grid-template-columns:1fr}.status-card{grid-template-columns:1fr auto}.card{padding:18px}}
 @media(max-width:520px){.radio-row,.status-options{grid-template-columns:1fr}}
@media(max-width:420px){.status-card{grid-template-columns:1fr}.status-pill{justify-self:start}.inline{grid-template-columns:1fr;gap:0}}
</style></head><body><main>
<header><div><div class="eyebrow">Self-hosted game monitor</div><h1>The Division 2 Status Watch</h1></div><div class="logo" aria-hidden="true">D2</div></header>
<section class="card status-card" aria-live="polite"><div><div class="label">Ubisoft server status</div><div id="status-name" class="status-name">Checking…</div><div id="status-sub" class="status-sub">Waiting for the first check</div></div><div id="status-pill" class="status-pill unknown">Unknown</div></section>
<div class="columns"><section class="card"><h2>Monitor settings</h2><p class="intro">Choose how often to check, which statuses to alert for, and where to send notifications.</p>
<form id="settings-form"><div class="field"><label for="interval">Check interval</label><select id="interval" name="interval_minutes">
<option value="3">Every 3 minutes</option><option value="5">Every 5 minutes</option><option value="10">Every 10 minutes</option><option value="15">Every 15 minutes</option><option value="20">Every 20 minutes</option><option value="25">Every 25 minutes</option><option value="30">Every 30 minutes</option><option value="60">Every 60 minutes</option>
</select></div><div class="field"><div class="section-title">Status change alerts</div><div class="hint">Send a notification when Ubisoft changes to any selected status. Online (Operational) is selected by default.</div><div class="status-options"><label class="status-option"><input type="checkbox" name="notify_statuses" value="operational"> Online (Operational)</label><label class="status-option"><input type="checkbox" name="notify_statuses" value="maintenance"> Maintenance</label><label class="status-option"><input type="checkbox" name="notify_statuses" value="problems"> Problems</label><label class="status-option"><input type="checkbox" name="notify_statuses" value="outage"> Outage</label></div></div><div class="field"><label>Notification delivery</label><div class="radio-row"><label class="radio"><input type="radio" name="method" value="email"> Email</label><label class="radio"><input type="radio" name="method" value="sms"> Phone message</label><label class="radio"><input type="radio" name="method" value="gotify"> Gotify push</label></div></div>
<div id="email-settings" class="section" hidden><div class="section-title">Email delivery</div>
<div class="field"><label for="email_to">Send alerts to</label><input id="email_to" name="email_to" type="email" autocomplete="email" placeholder="you@example.com"></div>
<div class="inline"><div class="field"><label for="smtp_host">SMTP server</label><input id="smtp_host" name="smtp_host" placeholder="smtp.example.com"></div><div class="field"><label for="smtp_port">SMTP port</label><input id="smtp_port" name="smtp_port" type="number" min="1" max="65535" placeholder="587"></div></div>
<div class="inline"><div class="field"><label for="smtp_security">Connection security</label><select id="smtp_security" name="smtp_security"><option value="starttls">STARTTLS (usually 587)</option><option value="ssl">SSL/TLS (usually 465)</option><option value="none">None</option></select></div><div class="field"><label for="email_from">Sender email</label><input id="email_from" name="email_from" type="email" placeholder="alerts@example.com"></div></div>
<div class="field"><label for="smtp_username">SMTP username</label><input id="smtp_username" name="smtp_username" autocomplete="username" placeholder="Optional if relay is allowed"></div>
<div class="field"><label for="smtp_password">SMTP password</label><input id="smtp_password" name="smtp_password" type="password" autocomplete="new-password" placeholder="Leave blank to keep saved password"><div class="hint">A mail provider may require an app password. Saved passwords are never shown here.</div></div></div>
<div id="sms-settings" class="section" hidden><div class="section-title">Phone messages with Twilio</div>
<div class="field"><label for="phone_to">Your phone number</label><input id="phone_to" name="phone_to" type="tel" autocomplete="tel" placeholder="+15551234567"><div class="hint">Use international format, including + and country code.</div></div>
<div class="field"><label for="twilio_account_sid">Twilio Account SID</label><input id="twilio_account_sid" name="twilio_account_sid" autocomplete="off" placeholder="AC…"></div>
<div class="field"><label for="twilio_auth_token">Twilio Auth Token</label><input id="twilio_auth_token" name="twilio_auth_token" type="password" autocomplete="new-password" placeholder="Leave blank to keep saved token"></div>
<div class="field"><label for="twilio_from">Twilio sender phone number</label><input id="twilio_from" name="twilio_from" type="tel" placeholder="+15557654321"></div></div>
<div id="gotify-settings" class="section" hidden><div class="section-title">Gotify push notification</div>
<div class="field"><label for="gotify_server">Gotify server URL</label><input id="gotify_server" name="gotify_server" type="url" placeholder="https://gotify.example.com"><div class="hint">Enter the server address, optionally including a base path. You can also paste the full address ending in /message.</div></div>
<div class="field"><label for="gotify_app_token">Gotify application token</label><input id="gotify_app_token" name="gotify_app_token" type="password" autocomplete="new-password" placeholder="Leave blank to keep saved token"><div class="hint">Create an application in Gotify and copy its application token. The token is stored privately and never shown again here.</div></div>
<div class="field"><label for="gotify_priority">Notification priority</label><select id="gotify_priority" name="gotify_priority"><option value="0">0 — No notification</option><option value="1">1 — Quiet</option><option value="2">2 — Quiet</option><option value="3">3 — Quiet</option><option value="4">4 — Sound</option><option value="5">5 — Sound (default)</option><option value="6">6 — Sound</option><option value="7">7 — Sound</option><option value="8">8 — Sound and vibration</option><option value="9">9 — Sound and vibration</option><option value="10">10 — Sound and vibration</option></select></div>
<div class="actions"><button id="test-gotify-button" class="secondary" type="button">Send test push</button><div id="gotify-test-message" class="message" role="status"></div></div></div>
<div class="actions"><button id="save-button" type="submit">Save settings</button><div id="message" class="message" role="status"></div></div></form></section>
<aside class="card"><h2>Monitor details</h2><p class="intro">Alerts are sent once when Ubisoft changes to a status you selected.</p>
<div class="info-list"><div class="info-row"><span>Last checked</span><strong id="last-checked">—</strong></div><div class="info-row"><span>Last status change</span><strong id="last-change">—</strong></div><div class="info-row"><span>Alert method</span><strong id="alert-method">Not set</strong></div></div>
<div id="monitor-note" class="notice">If Ubisoft's page cannot be reached, the dashboard shows Unknown. If no unambiguous live status is found, it displays Online as the default and waits for the next check.</div>
<a class="source" href="https://www.ubisoft.com/en-us/game/the-division/the-division-2/status" target="_blank" rel="noreferrer">Open Ubisoft status page ↗</a>
<div class="actions"><button id="check-button" class="secondary" type="button">Check now</button></div></aside></div></main>
<script>
const form=document.getElementById('settings-form'),message=document.getElementById('message');
const labels={operational:'Online (Operational)',online:'Online',maintenance:'Maintenance',problems:'Problems',outage:'Outage',unknown:'Unknown'};
function time(v){if(!v)return '—';const d=new Date(v);return Number.isNaN(d.getTime())?v:d.toLocaleString()}
function method(v){document.getElementById('email-settings').hidden=v!=='email';document.getElementById('sms-settings').hidden=v!=='sms';document.getElementById('gotify-settings').hidden=v!=='gotify';for(const r of document.querySelectorAll('input[name="method"]'))r.checked=r.value===v}
function render(data,syncSettings=true){
  const s=data.state||{},c=data.settings||{},st=s.status||'unknown';
  const fallbackOnline=st==='unknown'&&String(s.error||'').toLowerCase().includes('no unambiguous live status found');
  const shownStatus=fallbackOnline?'online':st;
  document.getElementById('status-name').textContent=labels[shownStatus]||'Unknown';
  document.getElementById('status-pill').textContent=labels[shownStatus]||'Unknown';
  document.getElementById('status-pill').className='status-pill '+shownStatus;
  document.getElementById('status-sub').textContent=s.last_checked?(s.notification_error||(fallbackOnline?'No unambiguous live status found; showing default Online.':s.error||('Updated '+time(s.last_checked)))):'Waiting for first check';
  document.getElementById('last-checked').textContent=time(s.last_checked);
  document.getElementById('last-change').textContent=time(s.last_change);
  if(!syncSettings)return;
  document.getElementById('alert-method').textContent=c.method==='email'?'Email':c.method==='sms'?'Phone message':c.method==='gotify'?'Gotify push':'Not set';
  document.getElementById('interval').value=String(c.interval_minutes||5);
  method(c.method||'none');
  for(const k of ['email_to','smtp_host','smtp_port','smtp_security','smtp_username','email_from','phone_to','twilio_account_sid','twilio_from','gotify_server','gotify_priority'])if(c[k]!==undefined)document.getElementById(k).value=c[k];
  const selected=Array.isArray(c.notify_statuses)?c.notify_statuses:['operational'];
  for(const input of document.querySelectorAll('input[name="notify_statuses"]'))input.checked=selected.includes(input.value);
  if(c.has_smtp_password)document.getElementById('smtp_password').placeholder='Saved; leave blank to keep';
  if(c.has_twilio_auth_token)document.getElementById('twilio_auth_token').placeholder='Saved; leave blank to keep';
  if(c.has_gotify_app_token)document.getElementById('gotify_app_token').placeholder='Saved; leave blank to keep';
}
for(const radio of document.querySelectorAll('input[name="method"]'))radio.addEventListener('change',()=>method(radio.value));
let settingsLoaded=false;
async function refresh(){const r=await fetch('/api/status');if(!r.ok)throw Error('Could not load status');render(await r.json(),!settingsLoaded);settingsLoaded=true}
form.addEventListener('submit',async e=>{e.preventDefault();const data=Object.fromEntries(new FormData(form).entries());data.notify_statuses=[...form.querySelectorAll('input[name="notify_statuses"]:checked')].map(input=>input.value);data.method=form.querySelector('input[name="method"]:checked')?.value||'none';if(data.method!=='email')for(const k of ['email_to','smtp_host','smtp_port','smtp_security','smtp_username','smtp_password','email_from'])delete data[k];if(data.method!=='sms')for(const k of ['phone_to','twilio_account_sid','twilio_auth_token','twilio_from'])delete data[k];if(data.method!=='gotify')for(const k of ['gotify_server','gotify_app_token','gotify_priority'])delete data[k];const b=document.getElementById('save-button');b.disabled=true;message.className='message';message.textContent='Saving…';try{const r=await fetch('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});const v=await r.json();if(!r.ok)throw Error(v.error||'Could not save settings');render(v);message.className='message ok';message.textContent='Settings saved.';document.getElementById('smtp_password').value='';document.getElementById('twilio_auth_token').value='';document.getElementById('gotify_app_token').value=''}catch(err){message.className='message error';message.textContent=err.message}finally{b.disabled=false}});
document.getElementById('check-button').addEventListener('click',async e=>{const b=e.currentTarget;b.disabled=true;b.textContent='Checking…';try{const r=await fetch('/api/check',{method:'POST'});render(await r.json(),false)}catch(err){message.className='message error';message.textContent=err.message}finally{b.disabled=false;b.textContent='Check now'}});
document.getElementById('test-gotify-button').addEventListener('click',async e=>{const b=e.currentTarget,result=document.getElementById('gotify-test-message'),data=Object.fromEntries(new FormData(form).entries());b.disabled=true;b.textContent='Sending…';result.className='message';result.textContent='Sending test push…';try{const r=await fetch('/api/test-gotify',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});const v=await r.json();if(!r.ok)throw Error(v.error||'Gotify test failed');result.className='message ok';result.textContent=v.message||'Test push accepted by Gotify.'}catch(err){result.className='message error';result.textContent=err.message}finally{b.disabled=false;b.textContent='Send test push'}});
refresh().catch(err=>document.getElementById('status-sub').textContent=err.message);setInterval(()=>refresh().catch(()=>{}),15000);
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    server_version = "Division2StatusWatch/1.0"

    def log_message(self, fmt, *args):
        log.info("%s - %s", self.client_address[0], fmt % args)

    def authorized(self) -> bool:
        supplied = self.headers.get("Authorization", "")
        expected = "Basic " + base64.b64encode(f"{WEB_USERNAME}:{WEB_PASSWORD}".encode()).decode("ascii")
        if supplied == expected:
            return True
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="The Division 2 Status Watch"')
        self.send_header("Content-Length", "0")
        self.end_headers()
        return False

    def json_response(self, code: int, value: dict):
        body = json.dumps(value).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not self.authorized():
            return
        if self.path == "/api/status":
            self.json_response(200, MONITOR.public_snapshot())
        elif self.path == "/background.png":
            body = BACKGROUND_PATH.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Cache-Control", "public, max-age=86400")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path in ("/", "/index.html"):
            body = INDEX_HTML.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_error(404)

    def do_POST(self):
        if not self.authorized():
            return
        if self.path not in ("/api/settings", "/api/check", "/api/test-gotify"):
            self.send_error(404)
            return
        try:
            if self.path == "/api/check":
                self.json_response(200, MONITOR.check_once())
                return
            length = min(int(self.headers.get("Content-Length", "0")), 65536)
            incoming = json.loads(self.rfile.read(length).decode())
            if self.path == "/api/settings":
                self.json_response(200, MONITOR.save_settings(incoming))
            else:
                try:
                    MONITOR.test_gotify(incoming)
                    self.json_response(200, {"message": "Gotify accepted the test push. Check your phone."})
                except ValueError as exc:
                    self.json_response(400, {"error": str(exc)})
                except Exception as exc:
                    log.warning("Gotify test push failed: %s", exc)
                    self.json_response(502, {"error": f"Gotify test failed: {exc}"})
        except (ValueError, json.JSONDecodeError) as exc:
            self.json_response(400, {"error": str(exc)})
        except Exception:
            log.exception("Request failed")
            self.json_response(500, {"error": "The request failed. Check the container logs."})


def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    log.info("The Division 2 Status Watch listening on port %s", PORT)
    server.serve_forever()


if __name__ == "__main__":
    main()
