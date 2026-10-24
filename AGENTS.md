# fpp-plugin-FalconBeat

FPP plugin: mirrors show audio to visitors' devices via a self-hosted
FalconBeat server. Sibling project `../FalconBeat-Server` holds the server, ingest
service and `bridge/` — the canonical protocol reference this plugin
replicates in Python (stdlib only, zero deps — FPP images ship python3 and
pip installs are system-wide across plugins).

## Layout

- `callbacks.py` — fppd entry point, keep it FAST (fppd waitpid()s on it)
- `falconbeatd.py` — the daemon: MiniWS client + FalconBeat session + control socket
- `js/qrcode.js` — vendored qrcode-generator (MIT © Kazuhiko Arase) for the
  signage QR on the config page; FPP auto-includes plugin `js/` files on
  every plugin page, exposing the global `qrcode`
- `settings.json` — declarative field/group schema FPP renders natively
  (PrintSettingGroup, per-field AJAX save, uiLevel-gated groups:
  0 Basic / 1 Advanced / 3 Developer)
- `content.php` / `status.php` / `about.php` + `menu.inc` — FPP UI
- `scripts/{fpp_install,fpp_uninstall,postStart,preStop}.sh` — lifecycle
- `pluginInfo.json` — strict JSON (no comments); `repoName` must equal the
  GitHub repo name. URLs point at `github.com/GraphicHealer/fpp-plugin-FalconBeat`.

## FPP callback contract (verified in fpp src/Plugins.cpp)

- fppd probes `callbacks{,.sh,.pl,.php,.py}` — `callbacks.py` is used.
- `callbacks.py --list` → `media,playlist,lifecycle` (10s answer timeout).
- `--type media --data '<json>'` — media JSON: `{type: both|media|
  sequence|pause|video|event, Sequence, Media, <id3 tags>}`. Blocking —
  fppd waitpid()s on the MAIN LOOP inside PreparePlay, so the callback
  must never wait on the daemon: it forwards the event over the control
  socket and exits (fire-and-forget). Events are also dropped unless the
  plugin is enabled AND serverAddr+token are set (`plugin_ready()`).
- `--type playlist --data '<json>'` — `Playlist::GetInfo()` + `{Action:
  start|playing|query_next|stop, Section: LeadIn|MainPlaylist|LeadOut,
  Item: int index within that section}` (PlaylistCallback::run @
  Plugins.cpp:1482). GetInfo() carries `name`, `currentState`,
  `currentEntry`, `size`, repeat/loop flags — **never the entry list**.
  The queue must come from `GET /api/playlist/<name>?mergeSubs=1`,
  which returns `leadIn|mainPlaylist|leadOut[]` with media under
  `.mediaName` (or `media.mediaFilename` / `.mediaFilename` on other
  entry shapes; `type:"playlist"` entries only expand via mergeSubs).
- `--type lifecycle startup|shutdown` — no --data. Waited max 5s.

## FalconBeat server protocol (server beatsync submodule @ 94c38ab)

One published port carries everything (server commit 5ef262b): `/ws` →
beatsync WS, `/health`, Bearer-gated `/upload/complete` + `/discover`,
ingest `PUT/DELETE /media/*` + `PUT /config/*` (token-checked by ingest
itself), public `GET /media/*`, and the listener SPA. `serverAddr` in
settings is the LAN edge (`host[:port]` or full URL — `_split_addr`
derives `wsBase`/`httpBase`). ONE shared secret, `falconbeatToken`
(= the server's FALCONBEAT_TOKEN), is the `Authorization: Bearer` upgrade
header on the admin WS AND the Bearer on `/upload/complete` AND ingest
writes — never a query string (proxy access logs would capture it).

- WS: `ws://host[:port]/ws?roomId&username&clientId` with header
  `Authorization: Bearer TOKEN` (server build accepts header or legacy
  `?creator=`; `?admin=` is demo-only). Plaintext JSON, not binary.
- Send `{"type":"PLAY","audioSource":url,"trackTimeSeconds":pos}` /
  `{"type":"PAUSE",...}` — the `SCHEDULED_ACTION` echo (match type +
  audioSource) is the ONLY acceptance signal; rejections are silent.
- `trackTimeSeconds` is the playback offset at execute time — send FPP
  position (sampled live at event time) + expected delay =
  `max(400, 1.5*maxClientRTT+200, maxCompensation+200)`ms capped 3s
  (we compute it from CLIENT_CHANGE stats + our own NTP RTT; `nudgeMs`
  setting adds on top) + `syncOffsetMs` (the user-facing tune knob).
  Devices land a schedule-delay late but at the RIGHT position — no
  gating of FPP, ever; a missed join is a late join at the same beat.
- Server load-coordination clock starts at PLAY: it waits ≤3s for client
  AUDIO_SOURCE_LOADED ACKs, then schedules regardless. Devices fetch audio
  over WAN — a freshly registered URL may not fit in 3s → they SYNC in
  late at the right offset. The lookahead usually has the file on-device
  well before its PLAY, so the load window is a safety net, not the plan.
- Push flow (no S3): `PUT <edge>/media/<name>` (Bearer TOKEN) →
  `POST <edge>/upload/complete {roomId, originalName, publicUrl}` with
  the same Bearer TOKEN (Caddy gates it — 403 without). The registered
  `publicUrl` is path-only (`/media/<name>`): the listener resolves it
  against the page origin, so devices always fetch through the ingress
  they arrived on and no public-base setting exists.
- Rolling lookahead, not bulk pre-push: the room only ever holds
  {current, next}. Playlist `start` stores the ordered queue (callback
  payload sections if present, else `GET /api/playlist/<name>` using the
  callback's `name`, then `current_playlist` in `/api/fppd/status`) and
  pushes entries 0+1 at start; each PLAY then
  advances the cursor and pushes `queue[idx+1]` in the background so
  devices download during the current track. Every PLAY also evicts registered sources outside
  {current, next} — played tracks, parked (paused) sources, AND tracks
  skipped mid-list all leave the room + get DELETE'd from ingest.
  Eviction pops `_pushed`/`_registered` atomically under `_reg_lock`
  AFTER the DELETE frame sends — an evicted source must never again be
  eligible for `_connect()`'s re-register recovery (which re-registers
  ONLY the {current, next} keep-set and evicts any other `_pushed`
  entry). A failed ingest file-DELETE goes to `_pending_file_dels` and
  retries on the next eviction.
  `query_next` fires when the current entry finishes (its `Item` is the
  finished index): it lazy-fetches the queue if start missed us, then
  pushes `queue[cursor+1]` — the true next entry.
  Playlist repeat: GetInfo() carries `repeat`/`loop`/`loopCount`; when
  another pass exists (`repeat && (loopCount==0 || loop+1<loopCount)`,
  core's own test in Playlist.cpp) the last entry's "next" is `queue[0]` —
  pushed, kept in the room, and advertised as nowplaying `next`.
  ensure_source stays the on-demand fallback for manual plays.
- Show metadata: plugin PUTs `{ingest}/config/show` (Bearer) whenever the
  rendered doc changes — FPP settings are the single source of truth (no
  web settings page on the server). `_show_doc()` builds it from the
  show* settings; links parse as `Label = URL` lines; showName/tagline
  default to FPP `/api/system/info` HostName/HostDescription. Re-checked
  every reconcile tick (~3s), so settings saves propagate automatically;
  skipped entirely while `enabled` is off.
- Now-playing metadata (server commit e0a318b): plugin PUTs
  `{ingest}/config/nowplaying` (Bearer) per media event —
  `{playing, title?, artist?, album?, filename, updatedAtMs}` — and
  `{playing:false}` on stop/pause/shutdown. Page treats `updatedAtMs` stale
  >90s as off air → daemon heartbeats the doc every 30s while playing.
  Rides plain ingest HTTP — no persistent WS needed for metadata.
- Listener count = non-admin clients in `CLIENT_CHANGE` (our creator socket
  is isAdmin → not counted). Surfaced in status.json → status.php.
- Ingest test endpoints: `GET /health` → {status:ok}; token probe =
  `PUT /config/show` with invalid JSON → 401 bad token / 400 good token
  (auth checked before parse; nothing written). The server has `/health` too.
- `LIVENESS_PING` → reply `LIVENESS_PONG`; room dies 60s after last client
  leaves, so the WS is held for the playlist duration only.
- NTP probes: `NTP_REQUEST {t0, probeGroupId, probeGroupIndex 0|1}` — pairs
  25ms apart, 50ms cadence for first 16, then 2.5s. Keep the min-RTT
  sample's offset.
- Room→source bindings are in-memory only → re-run `upload/complete` after
  any server restart (daemon does this on every (re)connect).

## Daemon ops notes (§ guidelines compliance)

- Nothing runs until configured: `enabled` defaults 0 and events/reconcile
  early-out on `configured(cfg)` = enabled + serverAddr + token all set.
  An unconfigure mid-run drops the admin session too.
- Single instance via `fcntl.flock(LOCK_EX|NB)` on `plugindata/.../
  falconbeatd.lock` held for process life — postStart, the lifecycle
  callback and the first event may all launch; extras exit cleanly. The
  unix control socket is 0600 (root↔root only).
- Only files under `media/music/` or `media/videos/` are uploadable —
  `find_media_file` realpaths and refuses traversal, subdirs, symlink
  escapes and loose files under media/ (the daemon is root).
- Uploads stream from disk (Content-Length set, http.client block-reads).
- Video entries: `_video_audio` extracts the soundtrack via ffmpeg into
  `plugindata/.../audio/` keyed on path+size+mtime — stream copy for
  aac/mp3 (bit-identical timing), `nice -n 19 -threads 1 -readrate 2` AAC
  transcode otherwise (refused while a show plays on single-core). Stored
  name = the extracted file's basename; ingest DELETE derives it from the
  room URL, not the media name.
- Idle cost ≈ 0: reconcile polls fppd every 3s while a playlist is active,
  30s otherwise; settings re-parse only on mtime/size change; status.json
  writes only on change (+60s freshness touch); server outages log once
  per failure class, then again on recovery.
- Logging goes through `WatchedFileHandler` so the daemon follows the file
  across FPP's logrotate (rename+create, not copytruncate); postStart and
  the respawn path send stdout/stderr to /dev/null so no held fd writes
  into the rotated .1.
- fppd status reconcile: the callbacks only fire on NEW transitions — a
  playlist already playing when the daemon comes up converges via the
  fppd-status poll (handlers dedupe on _current_media).

## Dev/testing

```bash
# Compile check
python -m py_compile falconbeatd.py callbacks.py

# Fake-stack integration test (works on Windows too — control channel falls
# back from unix socket to a localhost TCP port written to falconbeatd.port).
# The throwaway script isn't in the repo — recreate from fbtest.py (kept in
# the dev machine's temp dir): it spins up ONE fake edge (:18080 — WS +
# Bearer-gated HTTP + ingest), fake fppd (:13232), starts the daemon and
# drives playlist/media events through the control socket.
# Verified: queue fetch, start-window push, PLAY(pos+delay+offset), loop
# wrap, eviction + pending-delete retry, reconnect keep-set, stop cleanup,
# Bearer on every write, media-root/../ name refusal.
```

FPP settings live in `config/plugin.fpp-plugin-FalconBeat` (key=value);
settings.json fields save there via FPP's SetPluginSetting — EXCEPT the
credential field (`falconbeatToken`): it keeps the native
password row but override `onChange` to POST back to content.php, which
writes `plugindata/.../secrets` (0600) — §14.11 keeps credentials out of
config/ because the JSON backup carries plugin configs in clear. The daemon
reads plugin.* first, then secrets on top; a pre-migration token still in
plugin.* is used until the settings page migrates it. Daemon re-reads them
per event — no restart needed. Plugin log:
`logs/plugin-fpp-plugin-FalconBeat.log` (FPP rotates; keep exactly this name).
Daemon runtime state: `plugindata/fpp-plugin-FalconBeat/{falconbeatd.sock,
falconbeatd.pid,falconbeatd.lock,status.json,audio/}`.
