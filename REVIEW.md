@GraphicHealer thanks for the submission, and for fixing the automated findings so quickly.

Plugins are getting more complex, so we now use AI to do a first-round evaluation of each submission against the FPP Plugin Guidelines (`PLUGIN_GUIDELINES.md`, `PLUGININFO_FORMAT.md` and `PLUGINS.md`) before a maintainer reviews it by hand. What follows is that pass. I've read it through and am posting it for the maintainers, but I haven't independently checked every line, so please correct anything that's wrong.

Reviewed at `e7b2566`. FPP core cites are against current `master`.

What's already right: stdlib-only Python (no pip), no `sudo`, a unix-socket control channel rather than a TCP port, the token kept out of `config/` in a 0600 file and never echoed or logged, TLS verification left on, `restartFlag` set through `setSetting`, an uninstall that removes the plugin's config and `plugindata/`, and a Test button that shows the real curl error.

## Must fix before listing (risk to FPP, security, or a feature that doesn't work on an advertised version)

**1. The Sync Gate delays every track and freezes fppd while it waits** (submission acknowledgement: *"does not interfere with core FPP functionality, stability, or real-time sequence playback"*; §4 *Don't destabilize the host*) — `callbacks.py:164-171`, `settings.json:166-192`, `falconbeatd.py:1110-1250`

Syncing visitors' devices to the show is a good feature, and we'd like to list it. What can't be listed is the approach: holding FPP back until the devices are ready. The plugin has to keep up with FPP, not the other way round.

fppd runs the media callback inside `PlaylistEntryMedia::OpenMediaOutput()` (`src/playlist/PlaylistEntryMedia.cpp:497`) and waits for it with an unbounded `waitpid()` (`src/Plugins.cpp:1479`). That happens on the main loop, under `Player::Process` (`src/fppd.cpp:2288-2289`). While the callback is held:

- the next entry doesn't start, and for a `both` entry neither does its sequence;
- fppd processes no FPP commands, scheduler work, MultiSync control packets, timers or GPIO inputs, so a button or sensor trigger in the gap waits;
- the show drifts against its schedule by the total of every hold.

How long the hold lasts:

- **Healthy server:** the PAUSE and PLAY echo round trips plus up to 3.5 s of deliberate hold (`:1247-1250`). A freshly registered track adds the 2.5 s `preloadSettleMs` sleep (`:1177-1180`).
- **Server slow or dropping packets:** the daemon only replies after `_set_nowplaying` (10 s timeout, `:875`), the WS handshake (10 s, `:161`) and the upload (120 s, `:626`). So every track waits the full `gateMaxSec`, 10 s by default.
- **Maximum setting:** `gateMaxSec` goes up to 60 (`settings.json:175`). fppd's main-loop watchdog fires at 30 s (`src/fppd.cpp:1926`). It raises "fppd main loop is stalled … sync packets are not being processed" (`:1990`) and takes a gdb stack dump.
- **Daemon down:** `ensure_daemon()` can add up to 5 s more before the gate even starts (`callbacks.py:112-117`).

It's also a support problem. An operator who installs the plugin sees new pauses between playlist entries, and nothing on FPP's side points at the plugin. The only explanation is a line on the plugin's help page (`about.php:16-18`). Those reports would come to FPP as playback bugs.

**Fix:** the media callback hands the event to the daemon and returns straight away, every time, and FPP's playlist timing is the same with the plugin installed as without it. Remove `gateMaxSec` and `preloadSettleMs`, or at least their blocking.

One way to keep the sync without blocking is to predict when the next track starts instead of holding it. The lookahead already uploads the next track during the current one, and the daemon already polls fppd's status, so it knows how long the current track has left. A server command that means "start this source at server time T", sent a few seconds before the current track ends, would have devices start together with FPP. When a prediction misses, devices join late at the right offset, which the non-gated PLAY path already handles.

If the operator wants a gap between songs so devices can catch up, FPP already has one: a Pause entry in the playlist. It's visible in the playlist and it doesn't freeze fppd. The README could suggest it.

**2. The plugin is enabled by default and talks to `localhost` before it's configured** (§14.13) — `settings.json:53`, `falconbeatd.py:66,70`, `:971-977`, `:950-959`

`enabled` defaults to `1` and `serverAddr` defaults to `localhost`. On a fresh install the reconcile loop therefore PUTs `http://localhost/config/show`, which is FPP's own Apache, every 3 s. It gets a 404 each time and writes a warning to the plugin log every 3 s until someone configures the plugin. The same 3 s retry and warning happen whenever a configured server is down. `callbacks.py` also starts the daemon on every event whether or not the plugin is enabled.

**Fix:** nothing runs, polls or sends until `serverAddr` and the token are both set and `enabled` is on. A fresh install defaults to off. Repeated failures back off, and the log notes the outage once instead of every tick.

**3. Two daemons can start at fppd start** (§2.1, §2.6) — `scripts/postStart.sh:11`, `callbacks.py:140-145`, `falconbeatd.py:1621-1622`, `:1640-1641`, `:1700-1710`

Both `postStart.sh` and the `lifecycle startup` callback launch the daemon. `fppd.service` is `Type=simple`, so `ExecStartPost` runs while fppd is still loading plugins, and the two launches can overlap. The single-instance check is check-then-bind: `main()` pings the socket if it exists, and `run()` then unlinks any existing socket and binds its own. If both start before either has bound, the second one takes the socket and the PID file. The first keeps running its reconcile loop, which calls `play()` on its own admin connection (`:994-1008`), so the server sees duplicate uploads and PLAYs. `preStop.sh`/`fpp_uninstall.sh` kill only the PID in the file, so the first copy also survives an uninstall until fppd next stops, and its state writer recreates `plugindata/` (`:1606`). We found this by reading the code and haven't reproduced it on a device.

**Fix:** exactly one daemon runs, whichever launcher fires first. Use an atomic lock (for example `fcntl.flock` on a lock file held for the life of the process), never unlink a socket that a live daemon owns, and use a single launcher if the second one isn't needed.

**4. Uploads load the whole file into memory, and video entries are uploaded too** (§7) — `falconbeatd.py:616-617`, `:648`, `:1511`

`push_file()` does `f.read()` on the entire file. `find_media_file()` also searches `videos/`, and `_on_media()` treats `video` entries like audio. A few-hundred-MB video in a playlist is read into the (root) daemon's memory while fppd is playing, which can push a 512 MB board into the OOM killer. The visitors' devices then download the whole video for its soundtrack.

**Fix:** the upload streams from disk, so memory use doesn't depend on file size, and video entries play for listeners without the whole video being uploaded to the server and downloaded by every device.

A good way to get there is to extract each video's audio track once, ahead of time, and upload that instead. FPP images already include `ffmpeg`, and `ffmpeg -i show.mp4 -vn -c:a copy show.m4a` copies the audio without re-encoding. That's fast even on a single-core board, loses no quality, and keeps the timing identical to the video FPP plays. Some things to get right:
- Start it early: at playlist start, or as soon as a video enters the lookahead window, so it's done well before that track plays. If it isn't ready in time, pause devices for that entry rather than waiting for it.
- Try the copy first. Only re-encode if the video's audio is in a codec phones can't play. Re-encoding also adds a small timing offset (roughly 25–50 ms of encoder delay for MP3/AAC), which a copy doesn't.
- An automatic re-encode is fine on boards with two or more cores (`nproc >= 2`), run as `nice -n 19` with `-threads 1`. fppd's output threads run at realtime priority and its main loop at normal priority, so a nice-19 single-threaded encode yields to both. On a single-core board, a re-encode only runs when no playlist is playing; the copy is cheap enough to run any time.
- Limit how fast ffmpeg reads the source with `-readrate` (for example 2–4× realtime). `ionice` priorities only apply under the BFQ disk scheduler, which FPP boxes don't normally use, and reading a large video at full speed competes with fppd reading its sequence and media files from the same card.
- Keep the output in `plugindata/fpp-plugin-FalconBeat/` (§5), not `media/music/`. Key it to the source file's name, size and modification time so an edited video is converted again, and remove it on uninstall.
- The README and the privacy `sends` text say that audio extracted from video entries is sent too ("show audio files" reads as music files only).

**5. The root daemon uploads whatever file a media name points at** — `falconbeatd.py:646-652`, `:1626`

`find_media_file()` tries `media/music/<name>`, then `media/videos/<name>`, then `media/<name>`, and uploads the first file it finds. The name isn't checked, and the daemon runs as root. A name with `../` therefore reaches any file on the box, and a plain name like `settings` reaches FPP's own settings file, passwords included, without one. The uploaded file is then served publicly from the FalconBeat server's `/media/` path until it's evicted.

The name comes from fppd's media callback and status (that is, a playlist entry's media name), or from anything that writes to the control socket, which is mode 0666. Under FPP's default of no authentication this gives a LAN user nothing they couldn't already get. But the outcome is a root process copying arbitrary files to a public URL, and the fix is small.

**Fix:** only files that resolve (via `realpath`) to inside `music/` or `videos/` are ever uploaded. Anything else is refused and logged.

## Should fix (plugin functionality)

**6. Log file handle across rotation** — `falconbeatd.py:1694-1698`, `scripts/postStart.sh:11`

FPP's logrotate for `plugin-*.log` renames the file and recreates it (`create`, no `copytruncate`; `etc/logrotate.d/fpp_other_logs`). The guideline text (§1.2) says it uses `copytruncate`, which is wrong; that's our mistake and we'll correct it. As things stand, the daemon keeps writing to the renamed `.1` after a rotation, and to a deleted file after the next one compresses it. The log you'd need for a support request is then missing from FPP's log viewer, and the deleted file keeps using disk space until fppd restarts.

**Fix:** logging follows the file across rotation. `logging.handlers.WatchedFileHandler` is a drop-in replacement, and the `nohup` redirect in `postStart.sh` needs the same treatment or `/dev/null`.

**7. Constant SD writes and polling while idle** — `falconbeatd.py:1647-1649`, `:968-978`

`status.json` is rewritten every 2 s, and every 3 s the daemon re-parses both settings files, checks the show info and GETs `/api/fppd/status`. That runs around the clock, even with no playlist running. On a single-core board it's a constant cost taken from fppd, and the status file is only read while someone has the status page open.

**Fix:** the daemon does next to nothing while no show is running. For example:
- write the status file only when something changes, keep it in RAM (`/dev/shm`), or have `status.php` ask the daemon over the control socket instead;
- poll fppd every 3 s only while a playlist is active, and every 30–60 s otherwise. The playlist callbacks already announce starts and stops, so the poll is only a safety net;
- re-read the settings files only when their modification time changes.

## Optional (polish)

**8. Privacy block text** — `pluginInfo.json:18`, `:33`
- `sends[0].what` is 187 characters against the 100 cap, which the automated check also reported. Move the list of show-info fields to `other`.
- `systemChanges[1].what` ends in mojibake (`â€` bytes in the file, from a double-encoded em dash), and that shows up in the install dialog.
- When Show Name and Tagline are left blank, the daemon sends FPP's HostName and HostDescription (`falconbeatd.py:915-922`). That isn't "the show info you configure", so add a few words saying so.

**Fix:** every text is within its cap, the dialog renders cleanly, and the description of what's sent matches what's actually sent.

**9. Token in the WebSocket query string** — `falconbeatd.py:371-373`, `content.php:231-233`

`?creator=<token>` ends up in the access logs of any reverse proxy in front of the server, and that one token controls the whole room.

**Fix:** if the server can take the token in a header, send it that way. Otherwise mention it in the server's docs.

**10. README** — `README.md:29-35` says "Four fields are all it needs" but lists three.

---

When you've addressed these, reply here so a maintainer can look again. `/recheck` only re-runs the automated check, which covers the schema, the privacy block's structure and text lengths, but none of the items above. Reply here too if you think any of these is wrong.





# NEW REVIEW

@GraphicHealer thanks for turning this round so quickly, and for the detailed commit message. It made the re-check easy.

Same process as last time: this is an AI-assisted first pass against the FPP Plugin Guidelines, which I've read through and am posting for the maintainers. I haven't independently checked every line.

Reviewed at `26b03ed` (the two commits since `e7b2566`).

All ten items from the first round are addressed, and the code matches what the commit message says. The callback no longer waits on the daemon, and the gate settings are gone. The plugin is inert until it's enabled and configured. There's a single daemon, held by `flock`, and the control socket is 0600. Uploads stream from disk. Media lookup is limited to `music/` and `videos/`, and we checked that `../` and plain names like `settings` are refused. Logging survives rotation. The idle daemon is quiet. The token moved to a header, and the privacy text is fixed. Two things are left. One is a corner of item 1, and the other is in the new video extraction.

## Must fix before listing (risk to FPP, security, or a feature that doesn't work on an advertised version)

**1. The media/playlist callback can still hold fppd for up to 5 s** (item 1 from the first round; submission acknowledgement and §4 *Don't destabilize the host*) — `callbacks.py:131-134`, `:111-128`, `:172`

The normal path is fixed. But if the daemon isn't answering when an event fires (it crashed, was killed, or hasn't started yet), `ensure_daemon()` spawns it and then polls `daemon_alive()` for up to 5 s before returning. fppd is in `waitpid()` on the main loop for that whole time, so the next entry doesn't start and commands, MultiSync and GPIO wait. A plugin must not be able to block fppd in any situation, including after its own daemon has failed.

**Fix:** every callback returns immediately, whether or not the daemon is up. If the daemon needs starting, spawn it detached and return without waiting. That event can be dropped, because the daemon's reconcile loop already picks up a playlist that's already playing when it starts.

## Should fix (plugin functionality)

**2. Audio extraction fails for every video entry** — `falconbeatd.py:1194`, `:1196-1213`

ffmpeg writes to `<name>.m4a.part`, and ffmpeg picks the output format from the file extension, so it refuses the file. Running `_video_audio()` on a test MP4 (ffmpeg 7.1) fails with:

```
Unable to choose an output format for '…/audio/show.cc67f23579f1.m4a.part'; use a standard extension for the filename or specify the format manually.
```

So the play fails, and listeners get nothing for any video entry. The README and help page say video soundtracks are uploaded. The rest of the change is right: the whole video is no longer uploaded, and the cache location and key are what we asked for.

**Fix:** a video entry's soundtrack is extracted and plays for listeners. For example, put the real extension last (`<name>.part.m4a`) or pass `-f` to ffmpeg.

## Optional (polish)

None found.

---

When you've addressed these, reply here so a maintainer can look again. `/recheck` only re-runs the automated check, which covers the schema, the privacy block's structure and text lengths, but neither item above. Reply here too if you think either of these is wrong.
