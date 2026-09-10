#!/usr/bin/env python3
"""Mirror busy time between Microsoft 365 calendars as private "Busy" blocks."""
import argparse
import base64
import hashlib
import http.server
import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

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
        if not (a.get("client_id") or cfg.get("client_id")):
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


def cmd_sync(cfg, tokens, args):
    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    start, end = today - timedelta(days=1), today + timedelta(days=cfg.get("days_ahead", 60))
    mirror = set(cfg.get("mirror_show_as", DEFAULT_SHOW_AS))

    fetched, ok = {}, True
    for acct in cfg["accounts"]:
        try:
            token = access_token(cfg, acct, tokens)
            events = list_events(cfg, token, start, end)
            fetched[account_oid(tokens, acct)] = (acct["name"], token, events, acct.get("receive_blocks", True))
        except SyncError as e:
            ok = False
            log(f"{acct['name']}: {e}")

    meetings = merge_meetings({
        oid: [ev for ev in events if tag_of(ev) is None and not ev.get("isCancelled") and ev.get("showAs") in mirror]
        for oid, (_, _, events, _) in fetched.items()
    })

    for target, (name, token, events, receives) in fetched.items():
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
        oid = account_oid(tokens, acct)
    except SyncError as e:
        sys.exit(f"{acct['name']}: {e}")
    blocked = False
    for other in cfg["accounts"]:
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
