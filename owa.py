"""Outlook on the web transport for calsync: a calendar reached through a resident, signed-in Chromium."""
import fcntl
import json
import os
import signal
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path

ORIGIN = "https://outlook.cloud.microsoft"
CALENDAR_URL = ORIGIN + "/calendar/view/week"
HOME = Path(os.environ.get("CALSYNC_OWA_HOME", Path.home() / "Library/Application Support/calsync"))
PROFILE = HOME / "owa-profile"
LOCK = HOME / "owa.lock"
BROWSER_JOB = "io.github.zanakis.calsync.browser"
SESSION_HEADERS = ("authorization", "x-anchormailbox", "x-owa-sessionid", "x-client-version")
LOGIN_HOSTS = ("login.microsoftonline.com", "login.microsoft.com", "login.live.com")
CHROMIUM_FLAGS = [
    "--remote-debugging-port=0",
    "--no-first-run",
    "--no-default-browser-check",
    # The profile was created under Playwright, which encrypts cookies with this mock key instead of the login keychain.
    "--use-mock-keychain",
    "--password-store=basic",
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


def not_running(acct):
    return f"browser not running (run: calsync.py login {acct['name']})" if acct else "browser not running"


def needs_sign_in(acct):
    return f"needs sign-in (run: calsync.py login {acct['name']})"


def devtools_port():
    try:
        return int((PROFILE / "DevToolsActivePort").read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return None


def on_login_page(url):
    return (urllib.parse.urlsplit(url).hostname or "") in LOGIN_HOSTS


class Browser:
    """An attachment to the resident browser over CDP. Closing it detaches and leaves the browser running."""

    def __init__(self, error, acct=None):
        from playwright.sync_api import Error, sync_playwright
        self.Error = Error
        port = devtools_port()
        if not port:
            raise error(not_running(acct))
        self.pw = sync_playwright().start()
        try:
            self.browser = self.pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}", timeout=15000)
            self.ctx = self.browser.contexts[0]
            pages = [p for p in self.ctx.pages if not p.url.startswith("devtools://")]
            self.page = pages[0] if pages else self.ctx.new_page()
        except Error:
            self.pw.stop()
            raise error(not_running(acct))

    def set_window(self, state):
        cdp = self.ctx.new_cdp_session(self.page)
        try:
            window = cdp.send("Browser.getWindowForTarget")["windowId"]
            cdp.send("Browser.setWindowBounds", {"windowId": window, "bounds": {"windowState": state}})
        finally:
            cdp.detach()

    def window_state(self):
        cdp = self.ctx.new_cdp_session(self.page)
        try:
            return cdp.send("Browser.getWindowForTarget")["bounds"]["windowState"]
        finally:
            cdp.detach()

    def close(self):
        self.pw.stop()


def take_lock(error):
    HOME.mkdir(parents=True, exist_ok=True)
    fd = os.open(LOCK, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        raise error("another calsync run is using the browser")
    return fd


class Calendar:
    def __init__(self, cfg, acct, error, tag, creation_time):
        self.cfg, self.acct, self.error = cfg, acct, error
        self.tag, self.creation_time = tag, creation_time
        self.headers = None
        self.created = []
        self.lock = take_lock(error)
        try:
            self.browser = Browser(error, acct)
        except BaseException:
            os.close(self.lock)
            raise

    def close(self):
        try:
            self.browser.close()
        finally:
            os.close(self.lock)

    def authenticate(self):
        page = self.browser.page
        # Only a session that is already on Outlook is reloaded; a dead one is left on its sign-in page, so runs never touch auth.
        if not page.url.startswith(ORIGIN):
            raise self.error(needs_sign_in(self.acct))
        for _ in range(2):
            got = self.grab_headers(page)
            if got:
                self.headers = got
                return
        raise self.error("Outlook on the web didn't load")

    def grab_headers(self, page, timeout=60):
        got = {}

        def grab(req):
            h = req.headers
            if not got and req.url.startswith(ORIGIN + "/owa/service.svc") and h.get("authorization", "").startswith("Bearer"):
                got.update({k: h[k] for k in SESSION_HEADERS if k in h})

        ctx = self.browser.ctx
        ctx.on("request", grab)
        try:
            page.reload(wait_until="commit")
            end = time.time() + timeout
            while not got and time.time() < end:
                page.wait_for_timeout(500)
                if on_login_page(page.url):
                    raise self.error(needs_sign_in(self.acct))
        except self.browser.Error as e:
            raise self.error(f"browser error: {str(e).splitlines()[0][:200]}")
        finally:
            ctx.remove_listener("request", grab)
        return got

    def call(self, method, path, body=None):
        if self.headers is None:
            self.authenticate()
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
            try:
                status, text = self.browser.page.evaluate(FETCH, [ORIGIN + path, method, headers, data])
            except self.browser.Error as e:
                raise self.error(f"browser error: {str(e).splitlines()[0][:200]}")
            if status in (401, 403):
                if reauthenticated:
                    raise self.error(needs_sign_in(self.acct))
                reauthenticated = True
                self.headers = None
                self.authenticate()
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
        if r and r.get("iCalUId"):
            self.created.append(r["iCalUId"])

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


def start_browser(error):
    domain = f"gui/{os.getuid()}"
    if subprocess.run(["launchctl", "kickstart", f"{domain}/{BROWSER_JOB}"], capture_output=True).returncode:
        plist = Path.home() / "Library/LaunchAgents" / f"{BROWSER_JOB}.plist"
        if not plist.exists():
            raise error(f"browser not running, and {plist} isn't installed (see README)")
        subprocess.run(["launchctl", "bootstrap", domain, str(plist)], capture_output=True)
    end = time.time() + 60
    while time.time() < end:
        try:
            return Browser(error)
        except error:
            time.sleep(1)
    raise error("browser didn't start")


def login(acct, error, timeout=600):
    lock = take_lock(error)
    try:
        try:
            browser = Browser(error)
        except error:
            browser = start_browser(error)
        try:
            got = []

            def grab(req):
                if req.url.startswith(ORIGIN + "/owa/service.svc") and req.headers.get("authorization", "").startswith("Bearer"):
                    got.append(True)

            browser.ctx.on("request", grab)
            browser.set_window("normal")
            browser.page.bring_to_front()
            browser.page.goto(CALENDAR_URL, wait_until="commit")
            print(f"Sign in to {acct['name']} in the browser window that just opened. Waiting up to {timeout // 60} minutes.",
                  flush=True)
            end = time.time() + timeout
            while not got and time.time() < end:
                browser.page.wait_for_timeout(1000)
            if not got:
                raise error("sign-in timed out; the window stays open, run login again when ready")
            browser.page.wait_for_timeout(3000)
            browser.set_window("minimized")
        except browser.Error as e:
            raise error(f"browser error: {str(e).splitlines()[0][:200]}")
        finally:
            browser.close()
    finally:
        os.close(lock)


def run_browser():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        exe = p.chromium.executable_path
    PROFILE.mkdir(parents=True, exist_ok=True)
    os.chmod(HOME, 0o700)
    os.chmod(PROFILE, 0o700)
    (PROFILE / "DevToolsActivePort").unlink(missing_ok=True)
    # about:blank instead of Outlook: a fresh start has no session, and only a deliberate login should go to the sign-in page.
    proc = subprocess.Popen([exe, f"--user-data-dir={PROFILE}", *CHROMIUM_FLAGS, "about:blank"])
    signal.signal(signal.SIGTERM, lambda *_: proc.terminate())
    try:
        end = time.time() + 60
        while devtools_port() is None and proc.poll() is None and time.time() < end:
            time.sleep(0.5)
        try:
            browser = Browser(RuntimeError)
            try:
                browser.set_window("minimized")
            finally:
                browser.close()
        except Exception as e:
            print(f"couldn't minimize the browser window: {e}", file=sys.stderr, flush=True)
        return proc.wait()
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(10)
            except subprocess.TimeoutExpired:
                proc.kill()


if __name__ == "__main__":
    if sys.argv[1:] != ["browser"]:
        sys.exit("usage: owa.py browser")
    sys.exit(run_browser())
