# calsync

Copies busy time between Microsoft 365 calendars: `calsync.py` on the Mac, and an Android app in `android/` that does the same sync on a tap.

## UI walks

Walk the app with `~/.claude/bin/uiwalk` (`uiwalk help`). Flows live in
`uiwalk/<name>.flow`, app ids in `uiwalk/uiwalk.conf`. Start a flow that
tests a build with `install ${APP}`, because a walk gets whichever shared
device is free. One-off walks go inline. uiwalk takes the slot itself.
Build the APK with `./gradlew assembleDebug` in `android/` (`installDebug`
targets a phone over USB), then run
`uiwalk run smoke --var APP=android/app/build/outputs/apk/debug/app-debug.apk`
from the repo root.
