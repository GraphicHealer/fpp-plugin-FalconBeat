# fpp-plugin-FalconBeat

FPP plugin: mirrors show audio to visitors' phones via a self-hosted BeatSync
server. Sibling project `../FalconBeat-Server` holds the server, ingest
service and `bridge/` — the canonical protocol reference this plugin
replicates in Python (stdlib only, zero deps — FPP images ship python3 and
pip installs are system-wide across plugins).

## Layout

- `callbacks.py` — fppd entry point, keep it FAST (fppd waitpid()s on it)
- `falconbeatd.py` — the daemon: MiniWS client + BeatSync session + control socket
- `content.php` / `status.php` / `about.php` + `menu.inc` — FPP UI
- `scripts/{fpp_install,fpp_uninstall,postStart,preStop}.sh` — lifecycle
- `pluginInfo.json` — strict JSON (no comments); `repoName` must equal the
  GitHub repo name. URLs point at `github.com/GraphicHealer/fpp-plugin-FalconBeat`.

## FPP callback contract (verified in fpp src/Plugins.cpp)

- fppd probes `callbacks{,.sh,.pl,.php,.py}` — `callbacks.py` is used.
- `callbacks.py --list` → `media,playlist,lifecycle` (10s answer timeout).
- `callbacks.py --type media --data '<json>'` — media JSON: `{type: both|media|
  sequence|pause|video|event, Sequence, Media, <id3 tags>}`. Blocking.
- `--type playlist --data '<json>'` — full playlist object + `{Action:
  start|stop, Section, Item}`. Blocking. Playlist media lives under
  `leadIn|mainPlaylist|leadOut[].media.mediaFilename` (or `.mediaFilename`).
- `--type lifecycle startup|shutdown` — no --data. Waited max 5s.

## BeatSync protocol (beatsync @ 94c38ab)

- WS: `ws://host:8080/ws?roomId&username&clientId&creator=SECRET` (non-demo;
  `?admin=` is demo-only). Plaintext JSON messages, not binary.
- Send `{"type":"PLAY","audioSource":url,"trackTimeSeconds":pos}` /
  `{"type":"PAUSE",...}` — the `SCHEDULED_ACTION` echo (match type +
  audioSource) is the ONLY acceptance signal; rejections are silent.
- `trackTimeSeconds` is the playback offset at execute time — send FPP
  position + expected delay = `max(400, 1.5*maxClientRTT+200,
  maxCompensation+200)`ms capped 3s (we compute it from CLIENT_CHANGE stats
  + our own NTP RTT; `nudgeMs` setting adds on top).
- Server load-coordination clock starts at PLAY: it waits ≤3s for client
  AUDIO_SOURCE_LOADED ACKs, then schedules regardless. Phones fetch audio
  over WAN — a freshly registered URL may not fit in 3s → they SYNC in
  late at the right offset (observed: "phone starts ~3s late, synced").
  `preloadSettleMs` (default 2500) sleeps before PLAY on gated starts when
  the URL was registered within the settle window (tracked via
  `_registered_at`) — covers both a URL registered during that call and
  one the playlist-start pre-push registered seconds earlier; the gate
  holds FPP meanwhile (counts against gateMaxSec).
- Push flow (no S3): `PUT http://ingest:8090/media/<name>` (Bearer) →
  `POST http://beatsync:8080/upload/complete {roomId, originalName,
  publicUrl}` — phones then fetch `mediaPublicUrl/<name>` from Caddy.
  upload/complete is unauthenticated → stays LAN-only, never proxied.
- Rolling lookahead, not bulk pre-push: the room only ever holds
  {current, next}. Playlist `start` stores the ordered queue (callback
  payload top-level, then nested `playlist`/`Playlist`/`playlistData`
  objects, then `GET /api/playlist/<name>` by name — callback name keys
  first, then `current_playlist` in `/api/fppd/status`) and pushes
  entries 0+1 at start; each PLAY then
  advances the cursor and pushes `queue[idx+1]` in the background so
  phones download during the current track. Every PLAY also evicts registered sources outside
  {current, next} — played tracks, parked (paused) sources, AND tracks
  skipped mid-list all leave the room + get DELETE'd from ingest.
  `query_next` also fires an opportunistic ensure_source for its Item's
  mediaFilename (belt-and-suspenders even without a queue).
  ensure_source stays the on-demand fallback for manual plays.
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
  (auth checked before parse; nothing written). Beatsync has `/health` too.
- `LIVENESS_PING` → reply `LIVENESS_PONG`; room dies 60s after last client
  leaves, so the WS is held for the playlist duration only.
- NTP probes: `NTP_REQUEST {t0, probeGroupId, probeGroupIndex 0|1}` — pairs
  25ms apart, 50ms cadence for first 16, then 2.5s. Keep the min-RTT
  sample's offset.
- Room→source bindings are in-memory only → re-run `upload/complete` after
  any server restart (daemon does this on every (re)connect).

## Dev/testing

```bash
# Compile check
python -m py_compile falconbeatd.py callbacks.py

# Fake-stack integration test (works on Windows too — control channel falls
# back from unix socket to a localhost TCP port written to falconbeatd.port).
# The throwaway script isn't in the repo — recreate from fbtest.py (kept in
# the dev machine's temp dir): it
# spins up fake ingest (:18090), fake beatsync WS+HTTP (:18080), fake fppd
# status (:13232), starts the daemon and drives playlist/media events.
# Verified: PUT ingest, upload/complete, NTP pairs, LIVENESS_PONG,
# PLAY(fpp pos + delay), PAUSE on stop, graceful shutdown.
```

FPP settings live in `config/plugin.fpp-plugin-FalconBeat` (key=value);
daemon re-reads them per event — no restart needed. Plugin log:
`logs/plugin-fpp-plugin-FalconBeat.log` (FPP rotates; keep exactly this name).
Daemon runtime state: `plugindata/fpp-plugin-FalconBeat/{falconbeatd.sock,
falconbeatd.pid,status.json}`.
