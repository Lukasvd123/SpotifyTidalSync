# Scenarios

Every situation the app has to handle, what it must do, and what verifies it.
Automated IDs map to test names (`test_ST1_...`). Rows marked **manual** need
the real Spotify and Tidal apps and are run by hand before a release, using the
steps in the last section.

Run the automated suite with:

```bash
python -m pip install -r requirements.txt -r requirements-dev.txt
python -m pytest tests                    # unit, scenario, UI smoke
python -m pytest tests/integration        # real libmpv, silent null output
```

CI (`.github/workflows/tests.yml`) runs the suite on Linux, Windows and macOS
with Python 3.10, 3.12 and 3.13, plus the libmpv tests on Ubuntu 22.04 (the
oldest libmpv the packages ship), current Ubuntu and Windows.
`.github/workflows/release.yml` builds every package and runs its
`--self-test` on a clean system (Ubuntu 24.04 and Fedora containers for the
deb and rpm).

## How the scenario tests work

`tests/sim.py` runs the real `SyncEngine` and `PlaybackSource` against a fake
world on a fake clock: a muted Spotify with a queue, crossfade, repeat and
403-refused commands; a player that buffers, re-buffers on seek and can fail; a
Tidal catalogue whose masters can be longer or shorter than Spotify's. A
recorder samples who is audible every 50ms, so tests assert on what you would
actually hear: silence at boundaries, both players audible at once, and seeks
of our stream.

## Start of a track (no skipping around)

| ID | Scenario | Expected | Platforms | Verified by |
|----|----------|----------|-----------|-------------|
| ST1 | Spotify starts a song from 0 | Tidal starts at 0, zero seeks of our stream, Spotify pulled in step with it | all | test |
| ST2 | App starts while Spotify is mid-song | Tidal opens at that position (`:start-time`), no audible jump from 0 | all | test + IN2 |
| ST3 | Tidal takes 4s to buffer | Spotify is paused while we buffer, no catch-up seek afterwards | all | test |
| ST4 | Spotify Web API reports stale progress for 2s after our own seek | Not mistaken for a user seek | API | test |
| ST5 | OS position only updates on events (raw Windows SMTC) | Never moves our stream | Windows | test, DT8 |
| ST6 | Player reports position 0 forever | Never moves our stream; boundaries still work | Linux players | test |
| ST7 | Spotify is paused when the app starts | Waits, starts when Spotify plays | all | test |

## Track boundaries

| ID | Scenario | Expected | Platforms | Verified by |
|----|----------|----------|-----------|-------------|
| BD1 | Tidal master shorter than Spotify's (the gaps in old logs) | Muted Spotify is skipped forward, boundary silence ≤ 1.5s | all | test |
| BD2 | Spotify crossfades and flips metadata early, Tidal master longer | Our song plays to its real end, Spotify held, then next track | all | test |
| BD3 | Equal lengths, no crossfade | Clean switch, no skipped track | all | test |
| BD4 | Next track known from the API queue | Pre-buffered in a standby player, gapless switch | API | test + IN3 |
| BD5 | Repeat-one | Same track plays again | all | test |
| BD6 | Same track queued twice in a row | Plays twice, not skipped | all | test |
| BD7 | End of the queue | Goes idle, no loop, no crash | all | test |
| BD8 | Shorter master and a 12s crossfade | No gap, no skipped track | all | test |
| BD9 | Player clock lags the audio (libvlc on Tidal MP4: up to 13s by the end) | Real end is not taken for a dropped stream, the ending is never replayed | all | test |
| BD10 | Same, after a mid-song start | Next track starts from 0 | all | test |
| BD11 | AI DJ: the API hides the queue | No prefetch possible; ISRC releases fetched in parallel to keep the boundary short | API | test (BD9), **manual** |

## User actions

| ID | Scenario | Expected | Platforms | Verified by |
|----|----------|----------|-----------|-------------|
| UA1 | Skip in Spotify mid-song | Switch within ~3s | all | test |
| UA2 | Four quick skips | Only the final track is played; intermediate loads are abandoned | all | test |
| UA3 | Seek in Spotify | Followed exactly once, after a confirming reading | all | test |
| UA4 | Pause / resume in Spotify | Mirrored, no seeks, still aligned | all | test |
| UA5 | Next button in the app | Works through the command queue | all | test |
| UA6 | Seek slider in the app | Our stream and Spotify move once, no echo seek | all | test |
| UA7 | Previous (restart same track) | Our stream goes to 0 | all | test |
| UA8 | Volume slider | Sets the player volume, persisted | all | test, IN4 |

## Spotify modes and content

| ID | Scenario | Expected | Platforms | Verified by |
|----|----------|----------|-----------|-------------|
| SM1 | Podcast episode | Handed back to Spotify (unmuted), no Tidal search, no popup | all | test, DT5 |
| SM2 | Ad (free accounts) | Same as SM1 | all | test, DT5 |
| SM3 | AI DJ commentary | Our song finishes, then Spotify unmuted for the DJ, mirroring resumes after | API | test |
| SM4 | Playing on the phone / another device | Nothing plays here, no commands sent to that device | API | test, SR2-SR4 |
| SM5 | Local files | Detected as normal track changes | all | test, SR6 |
| SM6 | Japanese/Korean/Cyrillic titles without the API | Distinct tracks, distinct mappings | all | test, MA2 |
| SM7 | Shuffle | Works like any queue; prefetch follows the API queue | all | **manual** |
| SM8 | Private session / offline Spotify | Detection via OS still works | all | **manual** |
| SM9 | Spotify web player in a browser | Not supported (browser audio can't be muted per tab) | all | documented |

## Matching (search engine)

| ID | Scenario | Expected | Verified by |
|----|----------|----------|-------------|
| MA1-2 | Mapping keys | Latin keys unchanged (old mappings keep working); non-Latin titles get real keys | test |
| MA3 | "Anti-Hero" style titles | Hyphen kept in the search query | test |
| MA4 | Song not on Tidal, same artist has others | No match (never a different song) | test |
| MA5-7 | Live / remix / extended vs studio, remasters | Studio wins, remaster accepted, far-off runtimes rejected | test |
| MA8-10 | Collabs, "&"/"+"/"and" spellings, wrong artist | Any credited artist counts; wrong artist rejected | test |
| MA11-13 | Generic study titles ("Rain", "Focus") | Runtime within 4s required, closest runtime wins | test |
| MA14-17 | Instrumental-only on Tidal, vocal vs instrumental | Instrumental accepted only with exact artist and runtime; explicit requests honoured | test |
| MA18 | Same title on several lo-fi albums | Album breaks the tie | test |
| MA19 | Classical movements, composer vs performer | Matches on work, movement and runtime | test |
| MA20 | CJK titles | Match | test |
| MA23-24 | "Song 1" vs "Song 2", "Hello" vs "Hell", movement I vs II, "Part II" vs "Part 2" | Numbers and whole words must agree | test |
| TD3 | Spotify API gives an ISRC | Exact recording via ISRC, no text search | test |
| TD4 | Replaying a matched song | Cached match, no search | test |
| TD5 | Nothing found for title + artist | Retries with title + album, then title alone | test |

## Failures

| ID | Scenario | Expected | Platforms | Verified by |
|----|----------|----------|-----------|-------------|
| FL1 | No Tidal match | Spotify unmuted (you hear it), popup once per track, mirroring resumes on the next match | all | test, UI3 |
| FL2 | Stream drops mid-song | Reconnect at the same position | all | test |
| FL3 | Stream keeps failing | After 2 attempts fall back to Spotify audio | all | test |
| FL4 | Resume after a 20-minute pause (dead signed URL) | Reconnect at the paused position | all | test |
| FL5 | Match found but no stream at any quality | Fall back to Spotify audio, no popup | all | test |
| FL6 | Spotify API answers 403 at a boundary | OS media keys used instead | all | test, SR8 |
| FL7 | Offline at startup (autostart before network) | Tidal login retried with backoff | all | TD8 |
| FL8 | Track marked "Not on Tidal" | No search, no popup, Spotify audio | all | test, TD2 |
| FL9 | Window closed / SIGTERM / logout | Spotify unmuted and resumed | all | test (engine), **manual** (signals) |
| FL10 | "Not on Tidal" pressed in the popup | Remembered, falls back immediately | all | test |
| FL11 | Manual match from the popup | Mapping saved, track replaced | all | test |
| FL12 | App killed or crashed while Spotify muted | Next start unmutes it (marker file) | all | PL2 |
| FL13 | Expired Tidal access token | Refreshed before asking for a new login | all | TD9 |
| FL14 | One track has no hi-res master | Only that track drops to lossless | all | TD6-TD7 |
| FL15 | libmpv missing (source install) | Clear dialog with install hint, no raw crash | all | **manual** |
| FL16 | Corrupt settings file | Defaults used, broken file kept as `.corrupt` | all | PL5 |
| FL17 | Second instance | Refused, running instance untouched | all | PL7 |
| FL18 | Suspend / resume of the PC | Like FL4 | all | **manual** |

## Quality and output

| ID | Scenario | Expected | Verified by |
|----|----------|----------|-------------|
| QA1 | Login from an older version (device code) | Tidal sends AAC: labelled "AAC (lossy)", PKCE upgrade offered once at start | TD11, TD13-15 |
| QA2 | Hi-res master, PKCE login | DASH manifest played, label "FLAC 24-bit/…" | TD10, IN1-3 |
| QA3 | Volume 100, ReplayGain off, exclusive / forced rate | "bit-perfect" shown; mpv output format equals the source | IN8, **manual** |
| QA4 | Volume below 100 or ReplayGain on | Not shown as bit-perfect | IN8-9 |
| QA5 | Another app holds the Windows device exclusively | Falls back to shared mode instead of silence | **manual** |
| QA6 | Linux: other apps hold PipeWire at 48 kHz | Graph switches to the track's rate while playing | **manual** (verified on a KEF LSX II LT) |
| QA7 | Old libmpv in the Linux packages (Ubuntu 22.04: 0.34) | loadfile without index, ReplayGain via a filter | IN10, CI |

## Updates

| ID | Scenario | Expected | Verified by |
|----|----------|----------|-------------|
| UP1-3 | Newer / same / no release | Offered only when newer; checksums attached | test |
| UP4 | Release without asset digests | Checksums from `SHA256SUMS` | test |
| UP5 | deb/rpm or source install | Points to the release page, never replaces files | test |
| UP6 | Download does not match its checksum | Rejected, nothing replaced | test |
| UP7-8 | AppImage / running .exe | Replaced in place (the .exe via rename), new version started | test, **manual** |

## Platform specifics

| ID | Scenario | Expected | Verified by |
|----|----------|----------|-------------|
| LX1 | A browser playing video is listed before Spotify on D-Bus | Spotify is read and controlled | DT1-DT3 |
| LX2 | Spotify native, Flatpak or Snap | Detected via MPRIS, stream found for muting | DT4, PL1, **manual** |
| LX3 | PipeWire stream-restore re-mutes our player's stream | Our own stream (by PID) is unmuted; volume untouched | **manual** |
| LX4 | Seek without the API | MPRIS `SetPosition` | **manual** |
| WN1 | Another app is the "current" media session (browser) | Commands go to Spotify's session, not the browser | **manual** |
| WN2 | SMTC timeline goes stale | Position extrapolated from `last_updated_time` | DT8, ST5 |
| WN3 | Seek without the API | SMTC `try_change_playback_position_async` | **manual** |
| WN4 | HiDPI display | Crisp UI (per-monitor DPI awareness) | **manual** |
| WN5 | Non-Latin titles in the log | UTF-8 log, no encoding errors | PL6 |
| WN6 | Taskbar / title bar icon on light and dark themes | Visible on both (solid gradient tile) | **manual** |
| MC1 | macOS | Clear "not supported yet" dialog | **manual** |

## Manual release checklist

Run on a real machine with Spotify and a Tidal HiFi account. Keep
`debug.log` open (Settings > Logs) while doing it.

**Linux (and repeat on Windows):**

1. Start a playlist in Spotify from the first song. Expect no "Source seeked"
   or "Stream dropped" lines in the first minute.
2. Start the app while a song is ~1 minute in. Expect "Starting mid-song at".
3. Let three songs change on their own. Expect no "waiting for the source"
   and no audible gap; "Started pre-buffered track" with the API enabled.
4. Press next in Spotify four times quickly. Expect "Skipped past" lines and
   only the last song playing.
5. Pause for 15 minutes, then resume. Expect playback to continue at the same
   spot (possibly a "reconnecting" line).
6. Play a podcast episode. Expect Spotify's audio and no popup.
7. Play something on your phone with the app running. Expect "Playing on
   <phone>" and the phone not being paused.
8. Kill the app (`kill -9` / Task Manager) while playing, start it again. Expect
   "Previous run left the source muted; restoring it" and Spotify audible.
9. Play a song you know isn't on Tidal. Expect Spotify audible and one popup;
   press "Not on Tidal" and replay it: no popup.
10. Check the taskbar icon on a light and a dark theme.

**Windows only:**

11. Play a YouTube video in a browser, pause it, then play Spotify. Pausing
    from the app must pause Spotify, not the video.
12. With the Spotify API **off**, seek in Spotify. Expect the app to follow
    once, and no periodic "Source seeked" lines during normal playback.
13. Set display scaling to 150% and check the window is sharp.
