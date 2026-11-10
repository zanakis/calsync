# calsync

Copies busy time between your Microsoft 365 calendars. Each calendar gets private "Busy" blocks for the time you're booked in the others, so colleagues in every org see you as busy.

- Only the start, end and status are copied. Subject, attendees, location and body are not.
- Blocks are private, have no reminder and no attendees, so nobody gets an invite.
- Events marked busy, away (out of office) or tentative are copied. Free and "working elsewhere" are not.
- A meeting that's in more than one of your calendars (say, one invite sent to two of your addresses) counts once. It's matched by time plus the same meeting ID or subject. Calendars that already have it get no block, and each of the others gets a single one.
- The window is from yesterday to `days_ahead` days ahead.
- The tool only ever edits or deletes blocks it created. It tags them with a hidden property (the source account's Microsoft ID), and events you create yourself are never touched.
- There is no local state (apart from one list of block IDs for step 7). Everything is worked out from the calendars on each run, so the Mac and the Android app can both sync without getting in each other's way. A duplicate block can briefly appear and the next run removes it.
- Each device only manages blocks from the accounts it can read on that run. Blocks from an account that's failing, or that's only set up on the other device, are left alone.

There are two parts. `calsync.py` runs on the Mac every 5 minutes and needs only Python 3 (plus a small venv if one calendar is written through Outlook on the web, see step 7). The Android app in `android/` does the same sync when you tap a button.

## 1. Configure

```sh
cd ~/calsync
cp config.example.json config.json
```

The example is ready to use as-is: it signs in with a Microsoft first-party app ID (Microsoft Office) via a short device code, so you don't have to register anything in Azure. Edit `config.json`:

- `accounts`: one entry per org. `name` is any short label. `email` pre-fills the sign-in.
- `timezone`: a Windows time zone name. `Romance Standard Time` is Copenhagen, Paris or Brussels time; it's used to line up all-day events. **The Mac and the phone must use the same value**, and the app bakes it in when it's built.
- `client_id` and `auth` (top level): the example uses the Microsoft Office ID `d3590ed6-52b3-4102-aeff-aad2292ab01c` with `"auth": "device"`, which works in most orgs with no setup. If you register your own app (step 2), put its ID in `client_id` and set `"auth": "browser"` for in-browser sign-in. Both can be overridden per account. The shared first-party ID only ever touches your own calendars, but it leans on an app your org already trusts — if that isn't acceptable to you, register your own.
- `"receive_blocks": false` on an account makes it one-way: its busy time is still copied to the others, but it gets no blocks itself, and any it already has are removed on the next sync. Use it for a personal calendar. The app bakes this in when it's built, matched by email, so the Mac and the phone always agree.
- An account can also set its own `tenant` (a domain or tenant ID) if the default `common` doesn't work for it.
- `"ics_url"` on an account reads its calendar from a published ICS link instead of signing in (in Outlook on the web: **Settings**, **Calendar**, **Shared calendars**, **Publish a calendar**, then copy the ICS link). Use it when an org won't let you sign in. The account is then read-only: its busy time is copied to the others, it never gets blocks, and it needs no `login`, unless it also has `"owa": true` (step 7). Publishing with only availability is enough, but then subjects can't be used to spot a meeting that's also in another calendar, only the meeting ID. Treat the link as a password. The Android app doesn't read ICS feeds, so on the phone that account is simply missing.

Rebuild the Android app after changing `client_id`, `auth`, `timezone`, `days_ahead` or any account, since it reads them at build time.

## 2. (Optional) Register your own app

Skip this unless you want in-browser sign-in instead of device codes, or the shared first-party ID is blocked in an org. In any org where you're allowed to register apps (by default every member is), go to https://entra.microsoft.com, then **App registrations**, then **New registration**:

- **Name:** `calsync`
- **Supported account types:** *Accounts in any organizational directory (Multitenant)*
- **Redirect URI:** platform *Public client/native (mobile & desktop)*, value `http://localhost`

Then:

- **Authentication**: set *Allow public client flows* to **Yes**.
- **API permissions**, then **Add**, then **Microsoft Graph**, then **Delegated**: add `Calendars.ReadWrite`.
- Copy the **Application (client) ID**, put it in `client_id`, and set `"auth": "browser"`.

## 3. Sign in to each account on the Mac

```sh
python3 calsync.py login org1
python3 calsync.py login org2
python3 calsync.py login org3
python3 calsync.py status
```

**If sign-in shows "Need admin approval":** that org wants an admin to approve the app before you can use it. Options:

- Try the other first-party ID: set `"client_id": "27922004-5251-4030-b22d-91ecd9a37ea4"` (Outlook) on that account. Some orgs allow one and not the other. A per-account `client_id` keeps device-code sign-in.
- Or register your own app (step 2) and use the **request approval** button on the consent screen, or ask that org's IT to approve it.

If sign-in fails with a "compliant device" or similar Conditional Access error, that org only allows managed devices, and nothing here helps from a personal machine. Only its IT can make an exception.

Sign-ins last as long as the tool runs regularly. If an account stays unused for about 90 days, or its org forces re-sign-in, `status` will tell you to log in again.

## 4. First sync

```sh
python3 calsync.py sync --dry-run
python3 calsync.py sync
```

## 5. Run automatically on the Mac

Edit `io.github.zanakis.calsync.plist` first and replace `YOUR_USERNAME` with your Mac username (three places), so the paths point at this folder. Then:

```sh
cp ~/calsync/io.github.zanakis.calsync.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/io.github.zanakis.calsync.plist
```

It runs every 5 minutes while the Mac is awake, and straight away when it wakes up. Changes and errors are logged to `~/calsync/calsync.log`.

Don't move this folder into Documents, Desktop or iCloud Drive. macOS blocks background jobs from reading those folders.

## 6. Android app

Build it and install it on a phone connected over USB:

```sh
cd ~/calsync/android
./gradlew installDebug
```

The build reads `../config.json`. Rebuild whenever you change `client_id`, `timezone` or `days_ahead`.

In the app:

- **Add account**: type the work email, then sign in. With the default `"auth": "device"` it shows a short code and a button to open microsoft.com/devicelogin; with `"auth": "browser"` it signs in on a Microsoft page in the app. Do this once per org. The phone keeps its own sign-ins, separate from the Mac's.
- **Sync now**: does the same as one run on the Mac, then shows what changed for each account.
- **Remove**: deletes that account's blocks from your other calendars, deletes the blocks the app added to its own calendar, and signs out of it.

Removing an account on the phone doesn't remove it from the Mac. If it's still in the Mac's `config.json`, the next Mac run recreates its blocks. To stop syncing an org completely, remove it in both places.

## 7. (Optional) Write blocks into a calendar through Outlook on the web

Some orgs block every app sign-in but still let you use Outlook on the web. For such an account, calsync can read it from its ICS feed (above) and write blocks into it through Outlook on the web's own session, in a browser that stays running on the Mac. Add `"owa": true` to that account, next to its `ics_url`, and drop any `"receive_blocks": false`. Only one account can do this.

The ICS feed and Outlook on the web are the same calendar, so calsync treats them as one account: a meeting already in it gets no block there, and the blocks calsync writes into it are recognised in the feed by their meeting ID and never copied back out. Those IDs are kept in `owa-blocks.json` (deleted blocks for 3 more days, since the feed lags). Don't delete that file while the calendar has blocks, or they're copied out once until the next run below puts it back.

Setup, once:

```sh
cd ~/calsync
python3 -m venv .venv
.venv/bin/pip install playwright==1.65.0
.venv/bin/playwright install chromium
sed "s/YOUR_USERNAME/$(whoami)/g" io.github.zanakis.calsync.browser.plist > ~/Library/LaunchAgents/io.github.zanakis.calsync.browser.plist
sed "s/YOUR_USERNAME/$(whoami)/g" io.github.zanakis.calsync.owa.plist > ~/Library/LaunchAgents/io.github.zanakis.calsync.owa.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/io.github.zanakis.calsync.browser.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/io.github.zanakis.calsync.owa.plist
python3 calsync.py login Darize
```

Recreate `.venv` after a Homebrew upgrade to a new Python version.

- The browser job keeps one Chromium running, minimized, with its own profile in `~/Library/Application Support/calsync/owa-profile`. launchd restarts it if it quits. It starts on a blank page and only goes to Outlook when you run `login`.
- `login` starts the browser job if it isn't running, then brings the window forward on Outlook's sign-in page. Sign in there yourself (a passkey works); the window minimizes again once Outlook has loaded.
- The sign-in lives only as long as that browser process. If the org doesn't offer "Stay signed in", a reboot or a crash signs you out, and `status` and the log show `needs sign-in` until you run `login` again. calsync doesn't save the session to get around that.
- The `owa` job runs `calsync.py sync --owa` every 15 minutes: it reads every calendar and writes blocks only into the Outlook on the web one. The plain 5-minute `sync` writes only the others and reads that account from its feed. Use `sync --owa --dry-run` to preview.
- When the session is gone or the browser isn't running, the run logs it and leaves the calendar alone. It never navigates to the sign-in page on its own, so nothing retries sign-in in the background.
- The browser is reachable by any program running as you through Chrome's debugging port on localhost, which is how calsync drives it.

To stop it: `launchctl bootout gui/$(id -u)/io.github.zanakis.calsync.owa` and `launchctl bootout gui/$(id -u)/io.github.zanakis.calsync.browser`, then remove both plists from `~/Library/LaunchAgents`.

## Removing one org on the Mac

```sh
python3 calsync.py remove org2
```

This deletes org2's blocks from every other calendar, deletes the blocks in org2's own calendar, and signs out of it. Then delete org2 from `config.json`.

## Stop and remove everything

```sh
launchctl bootout gui/$(id -u)/io.github.zanakis.calsync
rm ~/Library/LaunchAgents/io.github.zanakis.calsync.plist
python3 calsync.py clean --dry-run
python3 calsync.py clean
```

With step 7 set up, run `clean` before stopping the browser job, then boot out the `owa` and `browser` jobs the same way.

Uninstall the Android app too, otherwise a tap on Sync puts the blocks back.

`tokens.json` holds your sign-ins. Keep it private, and delete it to sign out.
