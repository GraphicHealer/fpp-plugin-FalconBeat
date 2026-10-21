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
  GitHub repo name. `homeURL`/`srcURL`/`bugURL` are placeholders until the
  repo exists.

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
- Push flow (no S3): `PUT http://ingest:8090/media/<name>` (Bearer) →
  `POST http://beatsync:8080/upload/complete {roomId, originalName,
  publicUrl}` — phones then fetch `mediaPublicUrl/<name>` from Caddy.
  upload/complete is unauthenticated → stays LAN-only, never proxied.
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
# back from unix socket to a localhost TCP port written to falconbeatd.port):
#   C:\Users\Garrett\AppData\Local\Temp\fbtest.py
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
