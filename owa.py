"""Outlook on the web transport for calsync: a calendar reached through a browser the user signs in to for one run."""
import fcntl
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.parse
from pathlib import Path

ORIGIN = "https://outlook.cloud.microsoft"
CALENDAR_URL = ORIGIN + "/calendar/view/week"
SIGN_OUT_URLS = (ORIGIN + "/owa/logoff.owa", "https://login.microsoftonline.com/common/oauth2/v2.0/logout")
HOME = Path(os.environ.get("CALSYNC_OWA_HOME", Path.home() / "Library/Application Support/calsync"))
LOCK = HOME / "owa.lock"
TMP = HOME / "owa-tmp"
MARKER = "--calsync-owa"
SESSION_HEADERS = ("authorization", "x-anchormailbox", "x-owa-sessionid", "x-client-version")
LOGIN_HOSTS = ("login.microsoftonline.com", "login.microsoft.com", "login.live.com")
SIGN_IN_TIMEOUT = 300
RUN_TIMEOUT = 600
BROWSER_ARGS = [
    MARKER,
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-sync",
    "--disable-search-engine-choice-screen",
    "--mute-audio",
    # A minimized window is otherwise throttled hard enough that Outlook's first request can take minutes.
    "--disable-background-timer-throttling",
    "--disable-renderer-backgrounding",
    "--disable-backgrounding-occluded-windows",
    "--window-size=1280,900",
]
FETCH = """async ([url, method, headers, body]) => {
  const r = await fetch(url, {method, headers, body: body == null ? undefined : body, credentials: 'include'});
  return [r.status, await r.text()];
}"""


def on_login_page(url):
    return (urllib.parse.urlsplit(url).hostname or "") in LOGIN_HOSTS


def leftover_pids():
    # Browser helpers don't carry the marker switch, but every process of the browser has the temp profile in its arguments.
    pids = set()
    for pattern in (MARKER, str(TMP)):
        out = subprocess.run(["pgrep", "-f", "--", pattern], capture_output=True, text=True).stdout
        pids |= {int(p) for p in out.split()}
    pids.discard(os.getpid())
    return pids


def kill_leftovers():
    pids = leftover_pids()
    for pid in pids:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    shutil.rmtree(TMP, ignore_errors=True)
    return pids


def take_lock(error):
    HOME.mkdir(parents=True, exist_ok=True)
    fd = os.open(LOCK, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        raise error("another calsync run is using Outlook on the web")
    return fd


def reap_orphans():
    """Kill a session browser left behind by a run that died. Returns the killed PIDs, or None while a run holds the lock."""
    try:
        fd = take_lock(RuntimeError)
    except RuntimeError:
        return None
    try:
        return kill_leftovers() if leftover_pids() or TMP.exists() else set()
    finally:
        os.close(fd)


class Session:
    """One headed, off-the-record browser for one run. Nothing of it outlives close(): no profile, port or saved state."""

    def __init__(self, error, acct):
        self.error, self.acct = error, acct
        self.lock = take_lock(error)
        self.pw = self.browser = self.ctx = None
        self.signed_in = False
        self.watchdog = threading.Timer(RUN_TIMEOUT, self.expire)
        self.watchdog.daemon = True
        try:
            self.watchdog.start()
            signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
            kill_leftovers()
            TMP.mkdir(mode=0o700)
            # Playwright's driver puts its temp profile and artifacts under TMPDIR, so they land in a folder calsync owns and wipes.
            from playwright.sync_api import Error, sync_playwright
            self.Error = Error
            saved = os.environ.get("TMPDIR")
            os.environ["TMPDIR"] = str(TMP)
            try:
                self.pw = sync_playwright().start()
            finally:
                if saved is None:
                    del os.environ["TMPDIR"]
                else:
                    os.environ["TMPDIR"] = saved
            self.browser = self.pw.chromium.launch(
                channel="msedge", headless=False, args=BROWSER_ARGS,
                ignore_default_args=["--enable-automation"])
            self.ctx = self.browser.new_context(no_viewport=True)
            self.page = self.ctx.new_page()
        except BaseException as e:
            self.close()
            if isinstance(e, Exception) and not isinstance(e, error):
                raise error(f"couldn't start the browser: {str(e).splitlines()[0][:200]}")
            raise

    def expire(self):
        print(f"{acct_name(self.acct)}: run took over {RUN_TIMEOUT // 60} minutes, stopped", file=sys.stderr, flush=True)
        kill_leftovers()
        os._exit(1)

    def capture_headers(self, navigate, timeout, stop_on_login):
        got = {}

        def grab(req):
            h = req.headers
            if not got and req.url.startswith(ORIGIN + "/owa/service.svc") and h.get("authorization", "").startswith("Bearer"):
                got.update({k: h[k] for k in SESSION_HEADERS if k in h})

        self.ctx.on("request", grab)
        try:
            navigate()
            end = time.time() + timeout
            while not got and time.time() < end:
                self.page.wait_for_timeout(500)
                if stop_on_login and on_login_page(self.page.url):
                    raise self.error("signed out during the run")
        except self.Error as e:
            raise self.error(f"browser error: {str(e).splitlines()[0][:200]}")
        finally:
            self.ctx.remove_listener("request", grab)
        return got

    def sign_in(self, timeout=SIGN_IN_TIMEOUT):
        def navigate():
            self.page.goto(CALENDAR_URL, wait_until="commit")
            self.page.bring_to_front()
            print(f"Sign in to {acct_name(self.acct)} in the browser window that just opened."
                  f" Waiting up to {timeout // 60} minutes.", flush=True)

        got = self.capture_headers(navigate, timeout, stop_on_login=False)
        if not got:
            raise self.error("sign-in timed out")
        self.signed_in = True
        return got

    def reload_headers(self):
        got = self.capture_headers(lambda: self.page.reload(wait_until="commit"), 60, stop_on_login=True)
        if not got:
            raise self.error("Outlook on the web didn't load")
        return got

    def evaluate(self, *args):
        try:
            return self.page.evaluate(*args)
        except self.Error as e:
            raise self.error(f"browser error: {str(e).splitlines()[0][:200]}")

    def sign_out(self, budget=5):
        end = time.time() + budget
        for url in SIGN_OUT_URLS:
            left = end - time.time()
            if left <= 0:
                return
            try:
                self.page.goto(url, wait_until="commit", timeout=left * 1000)
                self.page.wait_for_timeout(min(1000, max(0, (end - time.time()) * 1000)))
            except Exception:
                pass

    def close(self):
        try:
            for step in (self.sign_out if self.signed_in else None, getattr(self.ctx, "close", None),
                         getattr(self.browser, "close", None), getattr(self.pw, "stop", None)):
                if step:
                    try:
                        step()
                    except Exception:
                        pass
            self.ctx = self.browser = self.pw = None
        finally:
            kill_leftovers()
            self.watchdog.cancel()
            os.close(self.lock)


def acct_name(acct):
    return acct["name"] if acct else "Outlook on the web"


class Calendar:
    def __init__(self, cfg, acct, error, tag, creation_time, on_created=None):
        self.cfg, self.acct, self.error = cfg, acct, error
        self.tag, self.creation_time = tag, creation_time
        self.on_created = on_created
        self.session = Session(error, acct)
        try:
            self.headers = self.session.sign_in()
        except BaseException:
            self.session.close()
            raise

    def close(self):
        self.session.close()

    def call(self, method, path, body=None):
        reauthenticated = False
        attempt = 0
        while True:
            headers = dict(self.headers)
            headers["prefer"] = f'outlook.timezone="{self.cfg["timezone"]}", IdType="ImmutableId"'
            headers["x-req-source"] = "Calendar"
            data = None
            if body is not None:
                headers["content-type"] = "application/json; charset=utf-8"
                data = json.dumps(body)
            status, text = self.session.evaluate(FETCH, [ORIGIN + path, method, headers, data])
            if status in (401, 403):
                if reauthenticated:
                    raise self.error("Outlook on the web refused the session")
                reauthenticated = True
                self.headers = self.session.reload_headers()
                continue
            if status in (429, 500, 502, 503, 504) and attempt < 3:
                time.sleep(2 ** attempt)
                attempt += 1
                continue
            if method == "DELETE" and status == 404:
                return None
            if status >= 400:
                raise self.error(f"{method} {path.split('?')[0]} failed ({status}): {message(text)}")
            return json.loads(text) if text.strip() else None

    def list_events(self, start, end):
        query = {
            "startDateTime": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "endDateTime": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
            # Like Graph, Outlook drops the expanded properties unless $select includes Organizer.
            "$select": "Start,End,ShowAs,IsAllDay,IsCancelled,Subject,iCalUId,Organizer",
            "$expand": f"SingleValueExtendedProperties($filter=PropertyId eq '{self.tag}' or PropertyId eq '{self.creation_time}')",
            "$top": "250",
        }
        path = "/api/beta/me/calendarview?" + "&".join(f"{k}={urllib.parse.quote(v, safe='')}" for k, v in query.items())
        events = []
        while path:
            page = self.call("GET", path)
            events += page["value"]
            path = same_origin_path(page.get("@odata.nextLink"))
        if any(not ev.get("SingleValueExtendedProperties") for ev in events):
            raise self.error("Outlook didn't return block tags, so this calendar was left alone")
        return [graph_shape(ev) for ev in events]

    def create_block(self, src, start, end, show_as, all_day):
        r = self.call("POST", "/api/beta/me/events", {
            "Subject": self.cfg.get("block_subject", "Busy"),
            "Start": {"DateTime": start, "TimeZone": self.cfg["timezone"]},
            "End": {"DateTime": end, "TimeZone": self.cfg["timezone"]},
            "IsAllDay": all_day,
            "ShowAs": show_as[:1].upper() + show_as[1:],
            "Sensitivity": "Private",
            "IsReminderOn": False,
            "ResponseRequested": False,
            "SingleValueExtendedProperties": [{"PropertyId": self.tag, "Value": src}],
        })
        if r and r.get("iCalUId") and self.on_created:
            self.on_created(r["iCalUId"])

    def delete_event(self, event_id):
        self.call("DELETE", f"/api/beta/me/events/{urllib.parse.quote(event_id, safe='')}")


def message(text):
    try:
        return json.loads(text)["error"]["message"]
    except (ValueError, KeyError, TypeError):
        return text[:200]


def same_origin_path(link):
    if not link:
        return None
    parts = urllib.parse.urlsplit(link)
    return parts.path + ("?" + parts.query if parts.query else "")


def graph_shape(ev):
    show_as = ev.get("ShowAs") or "Unknown"
    return {
        "id": ev["Id"],
        "start": {"dateTime": ev["Start"]["DateTime"]},
        "end": {"dateTime": ev["End"]["DateTime"]},
        "showAs": show_as[:1].lower() + show_as[1:],
        "isAllDay": ev.get("IsAllDay"),
        "isCancelled": ev.get("IsCancelled"),
        "subject": ev.get("Subject"),
        "iCalUId": ev.get("iCalUId"),
        "singleValueExtendedProperties": [{"id": p["PropertyId"], "value": p.get("Value")}
                                          for p in ev.get("SingleValueExtendedProperties") or []],
    }
