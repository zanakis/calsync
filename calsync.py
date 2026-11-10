#!/usr/bin/env python3
"""Mirror busy time between Microsoft 365 calendars as private "Busy" blocks."""
import argparse
import base64
import hashlib
import http.server
import json
import os
import re
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.environ.get("CALSYNC_CONFIG", os.path.join(HERE, "config.json"))
TOKENS_PATH = os.environ.get("CALSYNC_TOKENS", os.path.join(HERE, "tokens.json"))
GRAPH = "https://graph.microsoft.com/v1.0"
SCOPES = "https://graph.microsoft.com/Calendars.ReadWrite offline_access openid profile"
TAG = "String {6f1c2a52-8e4b-4d8e-9a57-3c0f5b7d2e91} Name CalSyncSource"
DEFAULT_SHOW_AS = ["busy", "oof", "tentative"]
SHOW_AS_STRENGTH = ["oof", "busy", "tentative", "workingElsewhere", "free", "unknown"]
CREATION_TIME = "SystemTime 0x3007"


class SyncError(Exception):
    pass


def log(msg):
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}", flush=True)


def load_json(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        if default is None:
            sys.exit(f"Missing {path}")
        return default


def save_tokens(tokens):
    tmp = f"{TOKENS_PATH}.{os.getpid()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(tokens, f, indent=2)
    os.replace(tmp, TOKENS_PATH)


def load_config():
    cfg = load_json(CONFIG_PATH)
    cfg.setdefault("timezone", "UTC")
    names = [a["name"] for a in cfg["accounts"]]
    if len(set(names)) != len(names):
        sys.exit("Account names in config.json must be unique")
    for a in cfg["accounts"]:
        if not (a.get("client_id") or cfg.get("client_id") or a.get("ics_url")):
            sys.exit(f"No client_id set for account {a['name']!r}")
    return cfg


def account(cfg, name):
    for a in cfg["accounts"]:
        if name.lower() in (a["name"].lower(), a.get("email", "").lower()):
            return a
    names = ", ".join(a["name"] for a in cfg["accounts"])
    sys.exit(f"No account {name!r} in config.json. Use a name or email from it: {names}")


def client_id(cfg, acct):
    return acct.get("client_id") or cfg["client_id"]


def auth_base(acct):
    return f"https://login.microsoftonline.com/{acct.get('tenant', 'common')}/oauth2/v2.0"


def post_form(url, data):
    req = urllib.request.Request(url, data=urllib.parse.urlencode(data).encode())
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        try:
            return json.load(e)
        except ValueError:
            return {"error": f"HTTP {e.code}"}
    except urllib.error.URLError as e:
        return {"error": f"network error: {e.reason}"}


def auth_error(t):
    return (t.get("error_description") or t.get("error") or "unknown error").splitlines()[0]


def token_claims(t):
    try:
        payload = t["id_token"].split(".")[1]
        return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (KeyError, IndexError, ValueError):
        return {}


def store(tokens, name, t):
    prev = tokens.get(name, {})
    claims = token_claims(t)
    tokens[name] = {
        "user": claims.get("preferred_username") or prev.get("user"),
        "oid": claims.get("oid") or prev.get("oid"),
        "refresh_token": t.get("refresh_token") or prev["refresh_token"],
        "access_token": t["access_token"],
        "expires_at": time.time() + int(t.get("expires_in", 3600)) - 300,
    }
    save_tokens(tokens)


def account_oid(tokens, acct):
    oid = tokens.get(acct["name"], {}).get("oid")
    if not oid:
        raise SyncError(f"not signed in (run: calsync.py login {acct['name']})")
    return oid


def access_token(cfg, acct, tokens):
    entry = tokens.get(acct["name"])
    if not entry:
        raise SyncError(f"not signed in (run: calsync.py login {acct['name']})")
    if entry["expires_at"] > time.time():
        return entry["access_token"]
    t = post_form(f"{auth_base(acct)}/token", {
        "grant_type": "refresh_token",
        "client_id": client_id(cfg, acct),
        "refresh_token": entry["refresh_token"],
        "scope": SCOPES,
    })
    if "access_token" not in t:
        raise SyncError(f"{auth_error(t)} (run: calsync.py login {acct['name']})")
    store(tokens, acct["name"], t)
    return t["access_token"]


class CallbackHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        if "code" in params or "error" in params:
            self.server.params = params
            body = b"Signed in. You can close this tab."
            self.send_response(200)
        else:
            body = b""
            self.send_response(404)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def login_browser(cfg, acct, paste):
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)
    if paste:
        redirect = "http://localhost"
    else:
        server = http.server.HTTPServer(("127.0.0.1", 0), CallbackHandler)
        server.timeout = 1
        server.params = None
        redirect = f"http://localhost:{server.server_address[1]}"
    params = {
        "client_id": client_id(cfg, acct),
        "response_type": "code",
        "redirect_uri": redirect,
        "scope": SCOPES,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
        "prompt": "select_account",
    }
    if acct.get("email"):
        params["login_hint"] = acct["email"]
    url = f"{auth_base(acct)}/authorize?{urllib.parse.urlencode(params)}"

    if paste:
        print(f"Open this URL and sign in to {acct['name']}. You'll end on a page that fails to load;"
              f" that's expected. Copy its full address.\n\n{url}\n")
        reply = urllib.parse.parse_qs(urllib.parse.urlparse(input("Paste the address here: ").strip()).query)
    else:
        print(f"Opening your browser to sign in to {acct['name']}. If it doesn't open, visit:\n\n{url}\n")
        webbrowser.open(url)
        deadline = time.time() + 300
        while server.params is None and time.time() < deadline:
            server.handle_request()
        server.server_close()
        reply = server.params or {}

    got = {k: v[0] for k, v in reply.items()}
    if "error" in got:
        sys.exit(f"Sign-in failed: {got.get('error_description', got['error'])}")
    if got.get("state") != state or "code" not in got:
        sys.exit("Sign-in failed: no valid response received")
    return post_form(f"{auth_base(acct)}/token", {
        "grant_type": "authorization_code",
        "client_id": client_id(cfg, acct),
        "code": got["code"],
        "redirect_uri": redirect,
        "code_verifier": verifier,
        "scope": SCOPES,
    })


def login_device(cfg, acct):
    r = post_form(f"{auth_base(acct)}/devicecode", {"client_id": client_id(cfg, acct), "scope": SCOPES})
    if "device_code" not in r:
        sys.exit(f"Sign-in failed: {auth_error(r)}")
    print(r["message"])
    interval, deadline = int(r.get("interval", 5)), time.time() + int(r.get("expires_in", 900))
    while time.time() < deadline:
        time.sleep(interval)
        t = post_form(f"{auth_base(acct)}/token", {
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "client_id": client_id(cfg, acct),
            "device_code": r["device_code"],
        })
        if t.get("error") == "authorization_pending":
            continue
        if t.get("error") == "slow_down":
            interval += 5
            continue
        return t
    sys.exit("Sign-in timed out")


def retry_delay(e, attempt):
    try:
        return min(float(e.headers.get("Retry-After", "")), 60)
    except ValueError:
        return 2 ** attempt


def graph_message(detail):
    try:
        return json.loads(detail)["error"]["message"]
    except (ValueError, KeyError, TypeError):
        return detail[:200]


def graph(cfg, token, method, path, body=None):
    url = path if path.startswith("https://") else GRAPH + path
    headers = {"Authorization": f"Bearer {token}", "Prefer": f'outlook.timezone="{cfg["timezone"]}"'}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    for attempt in range(5):
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                raw = r.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")
            if e.code in (429, 500, 502, 503, 504) and attempt < 4:
                time.sleep(retry_delay(e, attempt))
                continue
            if method == "DELETE" and e.code == 404:
                return None
            raise SyncError(f"{method} {url.split('?')[0]} failed ({e.code}): {graph_message(detail)}")
        except urllib.error.URLError as e:
            if attempt < 4:
                time.sleep(2 ** attempt)
                continue
            raise SyncError(f"network error: {e.reason}")


def list_events(cfg, token, start, end):
    query = {
        "startDateTime": start.isoformat(timespec="seconds"),
        "endDateTime": end.isoformat(timespec="seconds"),
        # Graph silently drops the expanded properties unless $select includes organizer (or there is no $select).
        "$select": "start,end,showAs,isAllDay,isCancelled,subject,iCalUId,organizer",
        "$expand": f"singleValueExtendedProperties($filter=id eq '{TAG}' or id eq '{CREATION_TIME}')",
        "$top": "250",
    }
    path = "/me/calendarView?" + "&".join(f"{k}={urllib.parse.quote(v, safe='')}" for k, v in query.items())
    events = []
    while path:
        page = graph(cfg, token, "GET", path)
        events += page["value"]
        path = page.get("@odata.nextLink")
    # Every event has a creation time, so a missing one means the expansion was dropped and tags can't be trusted.
    if any(not ev.get("singleValueExtendedProperties") for ev in events):
        raise SyncError("Graph didn't return block tags, so this calendar was left alone")
    return events


def tag_of(ev):
    for prop in ev.get("singleValueExtendedProperties") or []:
        if prop["id"].lower() == TAG.lower():
            return prop["value"]
    return None


def tagged_blocks(cfg, token):
    now = datetime.now(timezone.utc)
    events = list_events(cfg, token, now - timedelta(days=360), now + timedelta(days=360))
    return [(ev["id"], tag_of(ev)) for ev in events if tag_of(ev) is not None]


def block_key(ev):
    start, end = ev["start"]["dateTime"][:19], ev["end"]["dateTime"][:19]
    all_day = bool(ev.get("isAllDay")) and start.endswith("T00:00:00") and end.endswith("T00:00:00")
    return start, end, ev["showAs"], all_day


def strength(show_as):
    return SHOW_AS_STRENGTH.index(show_as) if show_as in SHOW_AS_STRENGTH else len(SHOW_AS_STRENGTH)


def merge_meetings(sources):
    """Group copies of one meeting found in several calendars: same time, and same iCalUId or subject.

    Returns (accounts holding it, the single block key every other account should get) per meeting.
    """
    by_slot = defaultdict(list)
    for oid, events in sources.items():
        for ev in events:
            key = (oid,) + block_key(ev)
            ids = {(field, ev[field]) for field in ("iCalUId", "subject") if ev.get(field)}
            groups = by_slot[key[1], key[2], key[4]]
            keys = [key]
            for hit in [g for g in groups if g[0] & ids]:
                groups.remove(hit)
                ids |= hit[0]
                keys += hit[1]
            groups.append((ids, keys))
    return [({k[0] for k in keys}, min(keys, key=lambda k: (strength(k[3]), k[0])))
            for groups in by_slot.values() for _, keys in groups]


def delete_event(cfg, token, event_id):
    graph(cfg, token, "DELETE", f"/me/events/{urllib.parse.quote(event_id, safe='')}")


def create_block(cfg, token, src, start, end, show_as, all_day):
    graph(cfg, token, "POST", "/me/events", {
        "subject": cfg.get("block_subject", "Busy"),
        "start": {"dateTime": start, "timeZone": cfg["timezone"]},
        "end": {"dateTime": end, "timeZone": cfg["timezone"]},
        "isAllDay": all_day,
        "showAs": show_as,
        "sensitivity": "private",
        "isReminderOn": False,
        "responseRequested": False,
        "singleValueExtendedProperties": [{"id": TAG, "value": src}],
    })


WINDOWS_ZONES = {
    "UTC": "UTC", "Coordinated Universal Time": "UTC",
    "GMT Standard Time": "Europe/London", "Greenwich Standard Time": "Atlantic/Reykjavik",
    "W. Europe Standard Time": "Europe/Berlin", "Romance Standard Time": "Europe/Paris",
    "Central Europe Standard Time": "Europe/Budapest", "Central European Standard Time": "Europe/Warsaw",
    "E. Europe Standard Time": "Europe/Chisinau", "FLE Standard Time": "Europe/Kiev",
    "GTB Standard Time": "Europe/Bucharest", "Russian Standard Time": "Europe/Moscow",
    "Eastern Standard Time": "America/New_York", "Central Standard Time": "America/Chicago",
    "Mountain Standard Time": "America/Denver", "Pacific Standard Time": "America/Los_Angeles",
    "India Standard Time": "Asia/Kolkata", "China Standard Time": "Asia/Shanghai",
    "Tokyo Standard Time": "Asia/Tokyo", "AUS Eastern Standard Time": "Australia/Sydney",
}
BUSY_STATUS = {"BUSY": "busy", "OOF": "oof", "TENTATIVE": "tentative", "FREE": "free",
               "WORKINGELSEWHERE": "workingElsewhere"}
WEEKDAYS = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"]
GOID_PREFIX = bytes.fromhex("040000008200E00074C5B7101A82E008")
RRULE_PARTS = {"FREQ", "UNTIL", "COUNT", "INTERVAL", "BYDAY", "BYMONTHDAY", "BYMONTH", "BYSETPOS", "WKST"}


def zone(name):
    try:
        return ZoneInfo(WINDOWS_ZONES.get(name, name))
    except (ZoneInfoNotFoundError, ValueError):
        raise SyncError(f"unknown time zone {name!r}")


def is_ics(acct):
    return bool(acct.get("ics_url"))


def source_id(tokens, acct):
    # Prefixed so it can never collide with a Graph object ID, and sorts after them so a Graph copy of a shared meeting wins ties.
    if is_ics(acct):
        return "ics:" + (acct.get("email") or acct["name"]).lower()
    return account_oid(tokens, acct)


def fetch_ics(acct):
    req = urllib.request.Request(acct["ics_url"], headers={"User-Agent": "calsync"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            text = r.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        raise SyncError(f"ICS feed fetch failed (HTTP {e.code})")
    except (urllib.error.URLError, OSError) as e:
        raise SyncError(f"ICS feed fetch failed: {getattr(e, 'reason', e)}")
    if "BEGIN:VCALENDAR" not in text or "END:VCALENDAR" not in text:
        raise SyncError("ICS feed didn't return a complete calendar")
    return text


def ics_components(text):
    events, current, depth = [], None, 0
    for line in text.replace("\r\n", "\n").replace("\n ", "").replace("\n\t", "").split("\n"):
        if not line:
            continue
        name, _, value = line.partition(":")
        name, *params = name.split(";")
        name = name.upper()
        if name == "BEGIN":
            if value.upper() == "VEVENT" and current is None:
                current, depth = defaultdict(list), 0
            elif current is not None:
                depth += 1
        elif name == "END" and current is not None:
            if depth:
                depth -= 1
            elif value.upper() == "VEVENT":
                events.append(current)
                current = None
        elif current is not None and not depth:
            current[name].append((dict(p.split("=", 1) for p in params if "=" in p), value))
    return events


def ics_first(ev, name, default=None):
    return ev[name][0] if ev.get(name) else (None, default)


def ics_text(value):
    return re.sub(r"\\(.)", lambda m: "\n" if m.group(1) in "nN" else m.group(1), value or "")


def ics_time(params, value, local):
    """A DATE as a date, else an aware datetime (floating times are taken as local)."""
    value = value.strip()
    if params.get("VALUE", "").upper() == "DATE" or len(value) == 8:
        return datetime.strptime(value[:8], "%Y%m%d").date()
    dt = datetime.strptime(value[:15], "%Y%m%dT%H%M%S")
    if value.endswith("Z"):
        return dt.replace(tzinfo=timezone.utc)
    return dt.replace(tzinfo=zone(params["TZID"].strip('"')) if "TZID" in params else local)


def as_instant(t, local):
    if isinstance(t, datetime):
        return t.astimezone(timezone.utc)
    return datetime(t.year, t.month, t.day, tzinfo=local).astimezone(timezone.utc)


def month_days(year, month, byday, bymonthday):
    first = date(year, month, 1)
    last = (date(year + month // 12, month % 12 + 1, 1) - timedelta(days=1)).day
    days = set()
    for d in bymonthday:
        d = d if d > 0 else last + 1 + d
        if 1 <= d <= last:
            days.add(first.replace(day=d))
    for n, wd in byday:
        matches = [first.replace(day=d) for d in range(1, last + 1) if first.replace(day=d).weekday() == wd]
        if n == 0:
            days.update(matches)
        elif -len(matches) <= n <= len(matches) and n:
            days.add(matches[n - 1 if n > 0 else n])
    return days


def rrule_dates(rule, first, until_date):
    parts = dict(p.split("=", 1) for p in rule.upper().split(";") if "=" in p)
    if set(parts) - RRULE_PARTS or parts.get("FREQ") not in ("DAILY", "WEEKLY", "MONTHLY", "YEARLY"):
        raise SyncError(f"unsupported recurrence rule {rule!r}")
    freq, interval = parts["FREQ"], int(parts.get("INTERVAL", 1))
    byday = [(int(d[:-2] or 0), WEEKDAYS.index(d[-2:])) for d in parts.get("BYDAY", "").split(",") if d]
    bymonthday = [int(d) for d in parts.get("BYMONTHDAY", "").split(",") if d]
    bymonth = [int(m) for m in parts.get("BYMONTH", "").split(",") if m]
    setpos = [int(p) for p in parts.get("BYSETPOS", "").split(",") if p]
    wkst = WEEKDAYS.index(parts.get("WKST", "MO"))
    period = 0
    while True:
        if freq == "DAILY":
            day = first + timedelta(days=period * interval)
            if day > until_date:
                return
            cands = {day} if (not byday or day.weekday() in {wd for _, wd in byday}) else set()
        elif freq == "WEEKLY":
            week = first - timedelta(days=(first.weekday() - wkst) % 7) + timedelta(weeks=period * interval)
            if week > until_date:
                return
            wds = {wd for _, wd in byday} or {first.weekday()}
            cands = {week + timedelta(days=(wd - wkst) % 7) for wd in wds}
        elif freq == "MONTHLY":
            m = first.month - 1 + period * interval
            year, month = first.year + m // 12, m % 12 + 1
            if date(year, month, 1) > until_date:
                return
            cands = month_days(year, month, byday, bymonthday or ([] if byday else [first.day]))
        else:
            year = first.year + period * interval
            if date(year, 1, 1) > until_date:
                return
            cands = set()
            for month in bymonth or [first.month]:
                cands |= month_days(year, month, byday, bymonthday or ([] if byday else [first.day]))
        if bymonth:
            cands = {d for d in cands if d.month in bymonth}
        cands = sorted(cands)
        if setpos:
            cands = sorted({cands[p - 1 if p > 0 else p] for p in setpos if -len(cands) <= p <= len(cands) and p})
        for day in cands:
            if first <= day <= until_date:
                yield day
        period += 1
        if period > 100000:
            raise SyncError(f"recurrence rule {rule!r} doesn't end")


def ics_occurrences(ev, local, horizon):
    start = ics_time(*ics_first(ev, "DTSTART"), local)
    if ev.get("DTEND"):
        end = ics_time(*ev["DTEND"][0], local)
    else:
        end = start + timedelta(days=1) if not isinstance(start, datetime) else start
    if type(start) is not type(end):
        raise SyncError("ICS event mixes a date and a time in DTSTART/DTEND")
    if not ev.get("RRULE"):
        return [(start, end)]
    rule = ev["RRULE"][0][1]
    parts = dict(p.split("=", 1) for p in rule.upper().split(";") if "=" in p)
    count = int(parts["COUNT"]) if "COUNT" in parts else None
    stop = horizon
    if "UNTIL" in parts:
        stop = min(stop, as_instant(ics_time({}, parts["UNTIL"], start.tzinfo if isinstance(start, datetime) else local), local))
    excluded = {as_instant(ics_time(p, v, local), local)
                for p, values in ev.get("EXDATE", []) for v in values.split(",")}
    timed = isinstance(start, datetime)
    first = start.date() if timed else start
    length = end - start
    out = []
    for n, day in enumerate(rrule_dates(rule, first, stop.astimezone(start.tzinfo if timed else local).date() + timedelta(days=1))):
        if count is not None and n >= count:
            break
        occ = datetime.combine(day, start.timetz()) if timed else day
        instant = as_instant(occ, local)
        if instant > stop:
            break
        if instant not in excluded:
            out.append((occ, occ + length))
    return out


def graph_uid(uid, original):
    """The iCalUId Graph shows for this UID: Outlook's global object ID, dated for an occurrence of a series."""
    try:
        goid = bytearray.fromhex(uid)
    except ValueError:
        goid = None
    if not goid or not goid.startswith(GOID_PREFIX) or len(goid) < 40:
        raw = uid.encode() + b"\0"
        goid = bytearray(GOID_PREFIX + bytes(20) + (len(raw) + 12).to_bytes(4, "little") + b"vCal-Uid" + (1).to_bytes(4, "little") + raw)
    if original is not None:
        goid[16:20] = original.year.to_bytes(2, "big") + bytes([original.month, original.day])
    return goid.hex().upper()


def ics_events(cfg, acct, start, end):
    text = fetch_ics(acct)
    try:
        return parse_ics(cfg, text, start, end)
    except (ValueError, KeyError, IndexError, TypeError) as e:
        raise SyncError(f"couldn't read the ICS feed ({type(e).__name__}: {e})")


def parse_ics(cfg, text, start, end):
    local = zone(cfg["timezone"])
    components = [ev for ev in ics_components(text) if ev.get("DTSTART")]
    overridden = {(ics_text(ics_first(ev, "UID")[1]), as_instant(ics_time(*ev["RECURRENCE-ID"][0], local), local))
                  for ev in components if ev.get("RECURRENCE-ID")}
    events = []
    for ev in components:
        uid = ics_text(ics_first(ev, "UID")[1])
        busy = ics_first(ev, "X-MICROSOFT-CDO-BUSYSTATUS")[1]
        transparent = ics_first(ev, "TRANSP", "OPAQUE")[1].upper() == "TRANSPARENT"
        show_as = BUSY_STATUS.get((busy or "").upper(), "free" if transparent else "busy")
        for s, e in ics_occurrences(ev, local, end):
            if not ev.get("RECURRENCE-ID") and ev.get("RRULE") and (uid, as_instant(s, local)) in overridden:
                continue
            s_utc, e_utc = as_instant(s, local), as_instant(e, local)
            if not (s_utc < end and (e_utc > start or s_utc >= start)):
                continue
            all_day = not isinstance(s, datetime)
            if ev.get("RECURRENCE-ID"):
                original = ics_time(*ev["RECURRENCE-ID"][0], local)
            else:
                original = s if ev.get("RRULE") else None
            fmt = (lambda t: f"{t:%Y-%m-%d}T00:00:00.0000000") if all_day else \
                (lambda t: f"{t.astimezone(local):%Y-%m-%dT%H:%M:%S}.0000000")
            events.append({
                "start": {"dateTime": fmt(s)},
                "end": {"dateTime": fmt(e)},
                "showAs": show_as,
                "isAllDay": all_day,
                "isCancelled": ics_first(ev, "STATUS", "")[1].upper() == "CANCELLED",
                "subject": ics_text(ics_first(ev, "SUMMARY", "")[1]),
                "iCalUId": graph_uid(uid, original),
            })
    return events


def cmd_sync(cfg, tokens, args):
    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    start, end = today - timedelta(days=1), today + timedelta(days=cfg.get("days_ahead", 60))
    mirror = set(cfg.get("mirror_show_as", DEFAULT_SHOW_AS))

    fetched, ok = {}, True
    for acct in cfg["accounts"]:
        try:
            if is_ics(acct):
                fetched[source_id(tokens, acct)] = (acct["name"], None, ics_events(cfg, acct, start, end), False)
                continue
            token = access_token(cfg, acct, tokens)
            events = list_events(cfg, token, start, end)
            fetched[source_id(tokens, acct)] = (acct["name"], token, events, acct.get("receive_blocks", True))
        except SyncError as e:
            ok = False
            log(f"{acct['name']}: {e}")

    meetings = merge_meetings({
        oid: [ev for ev in events if tag_of(ev) is None and not ev.get("isCancelled") and ev.get("showAs") in mirror]
        for oid, (_, _, events, _) in fetched.items()
    })

    for target, (name, token, events, receives) in fetched.items():
        if token is None:
            continue
        wanted = Counter()
        if receives:
            wanted.update(key for holders, key in meetings if target not in holders)
        existing = defaultdict(list)
        for ev in events:
            src = tag_of(ev)
            # Blocks from accounts not read this run (failing, or only set up on another device) are left alone.
            if src in fetched:
                existing[(src,) + block_key(ev)].append(ev["id"])
        stale = [(key, i) for key, ids in existing.items() for i in ids[wanted[key]:]]
        missing = [key for key, n in wanted.items() for _ in range(n - len(existing.get(key, ())))]

        if stale or missing or args.verbose:
            log(f"{name}: +{len(missing)} -{len(stale)}" + (" (dry run)" if args.dry_run else ""))
        if args.verbose or args.dry_run:
            for src, s, e, show_as, _ in missing:
                print(f"    + {s} -> {e} {show_as} (from {fetched[src][0]})")
            for (src, s, e, show_as, _), _ in stale:
                print(f"    - {s} -> {e} {show_as} (from {fetched[src][0]})")
        if args.dry_run:
            continue

        for _, event_id in stale:
            try:
                delete_event(cfg, token, event_id)
            except SyncError as e:
                ok = False
                log(f"{name}: {e}")
        for key in missing:
            try:
                create_block(cfg, token, *key)
            except SyncError as e:
                ok = False
                log(f"{name}: {e}")
    return 0 if ok else 1


def wants_device(cfg, acct):
    # First-party workaround client IDs have no localhost redirect, so those accounts need the device flow.
    mode = acct.get("auth") or cfg.get("auth")
    return mode == "device" if mode else bool(acct.get("client_id"))


def cmd_login(cfg, tokens, args):
    acct = account(cfg, args.name)
    if is_ics(acct):
        sys.exit(f"{acct['name']} is read from its ICS feed and needs no sign-in")
    use_device = args.device or (not args.paste and wants_device(cfg, acct))
    t = login_device(cfg, acct) if use_device else login_browser(cfg, acct, args.paste)
    if "access_token" not in t:
        sys.exit(f"Sign-in failed: {auth_error(t)}")
    store(tokens, acct["name"], t)
    user = tokens[acct["name"]]["user"]
    print(f"Signed in to {acct['name']} as {user}")
    if acct.get("email") and user and user.lower() != acct["email"].lower():
        print(f"Note: config.json expects {acct['email']}. If that's a different account, run login again.")


def cmd_status(cfg, tokens, args):
    rc = 0
    for acct in cfg["accounts"]:
        try:
            if is_ics(acct):
                today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
                events = ics_events(cfg, acct, today - timedelta(days=1), today + timedelta(days=cfg.get("days_ahead", 60)))
                print(f"{acct['name']}: ICS feed OK, {len(events)} events in the sync window (read-only)")
                continue
            token = access_token(cfg, acct, tokens)
            graph(cfg, token, "GET", "/me/calendar?$select=name")
            print(f"{acct['name']}: OK ({tokens[acct['name']]['user']})")
        except SyncError as e:
            rc = 1
            print(f"{acct['name']}: {e}")
    return rc


def cmd_clean(cfg, tokens, args):
    rc = 0
    for name in args.names or [a["name"] for a in cfg["accounts"]]:
        acct = account(cfg, name)
        if is_ics(acct):
            print(f"{name}: ICS feed, nothing to clean in it")
            continue
        try:
            token = access_token(cfg, acct, tokens)
            ids = [event_id for event_id, _ in tagged_blocks(cfg, token)]
            print(f"{name}: {'would delete' if args.dry_run else 'deleting'} {len(ids)} blocks")
            if not args.dry_run:
                for event_id in ids:
                    delete_event(cfg, token, event_id)
        except SyncError as e:
            rc = 1
            print(f"{name}: {e}")
    return rc


def cmd_remove(cfg, tokens, args):
    acct = account(cfg, args.name)
    try:
        oid = source_id(tokens, acct)
    except SyncError as e:
        sys.exit(f"{acct['name']}: {e}")
    blocked = False
    for other in cfg["accounts"]:
        if is_ics(other):
            continue
        try:
            token = access_token(cfg, other, tokens)
            ids = [event_id for event_id, src in tagged_blocks(cfg, token) if other is acct or src == oid]
            print(f"{other['name']}: deleting {len(ids)} blocks")
            for event_id in ids:
                delete_event(cfg, token, event_id)
        except SyncError as e:
            print(f"{other['name']}: {e}")
            blocked = blocked or other is not acct
    if blocked:
        sys.exit("Some calendars couldn't be cleaned up. Fix that and run remove again.")
    if is_ics(acct):
        print(f"Removed {acct['name']}'s blocks. Now delete it from config.json.")
        return
    del tokens[acct["name"]]
    save_tokens(tokens)
    print(f"Signed out of {acct['name']}. Now delete it from config.json.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd")
    p = sub.add_parser("login", help="sign in to one account")
    p.add_argument("name")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--paste", action="store_true", help="paste the final URL by hand (use on phones)")
    mode.add_argument("--device", action="store_true", help="sign in with a code at microsoft.com/devicelogin")
    p = sub.add_parser("sync", help="mirror busy blocks between all accounts (default)")
    p.add_argument("--dry-run", action="store_true", help="show changes without making them")
    p.add_argument("-v", "--verbose", action="store_true")
    sub.add_parser("status", help="check that every account is signed in")
    p = sub.add_parser("remove", help="delete an account's blocks from every calendar and sign out of it")
    p.add_argument("name")
    p = sub.add_parser("clean", help="delete every block this tool created")
    p.add_argument("names", nargs="*", help="accounts to clean (default: all)")
    p.add_argument("--dry-run", action="store_true")

    args = parser.parse_args(sys.argv[1:] or ["sync"])
    cfg = load_config()
    tokens = load_json(TOKENS_PATH, {})
    handler = {"login": cmd_login, "sync": cmd_sync, "status": cmd_status,
               "remove": cmd_remove, "clean": cmd_clean}[args.cmd]
    sys.exit(handler(cfg, tokens, args) or 0)


if __name__ == "__main__":
    main()
