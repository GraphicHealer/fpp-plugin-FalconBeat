# fpp-plugin-FalconBeat

FPP (Falcon Player) plugin that mirrors the show's audio to visitors' phones
in sync, via a self-hosted [BeatSync](https://github.com/freeman-jiang/beatsync)
server (the FalconBeat stack).

## How it works

```
fppd ──media/playlist events──▶ callbacks.py ──unix socket──▶ falconbeatd
                                                                  │
        PUT /media/<file> (Bearer)            POST /upload/complete│
              ▼                                    ▼
        FalconBeat ingest :8090            BeatSync :8080 (LAN only)
                                                  │
                                  ws://…/ws?roomId&creator=SECRET
                                                  ▼
                                    PLAY / PAUSE with trackTimeSeconds
                                    = FPP position + computed delay
```

- `callbacks.py` — the file fppd calls (`--list`, `--type media|playlist
  --data <json>`, `--type lifecycle startup|shutdown`). fppd blocks on these
  calls, so it only forwards the event to the daemon and exits.
- `falconbeatd.py` — zero-dependency Python daemon. Holds the BeatSync admin
  WebSocket open for the duration of a playlist (a room with no clients is
  deleted after 60s), and keeps a rolling lookahead window: only the current
  and next playlist tracks are pushed to ingest + registered on the room, so
  phones download each track while the previous one plays instead of hauling
  the whole show at start. Played and skipped tracks are evicted from the
  room and deleted from ingest. It also PUTs `nowplaying.json` to ingest on
  every media/stop event (heartbeating every 30s while playing) so the
  listener page shows title/artist and an off-air banner, and reports the
  live non-admin listener count on the status page.
- `scripts/postStart.sh` / `preStop.sh` — start/stop the daemon with fppd.
- `content.php` — settings page (Content Setup → FalconBeat).
- `status.php` — live status (Status/Control → FalconBeat Status).

## Sync accuracy

The daemon runs the same NTP probe protocol as phones (coded probe pairs,
min-RTT offset kept) and tracks every client's RTT/compensation from
`CLIENT_CHANGE` room events. On gated playlist starts the media callback
holds FPP while phones load, sends PLAY with `trackTimeSeconds: 0`, and
releases the callback just before `serverTimeToExecute` — phones and FPP
start position 0 together, so the server's scheduling delay becomes a
pause *between* tracks instead of skipped intro audio. On non-gated
reconciles it sends FPP position + `max(400ms, 1.5·maxRTT+200,
maxCompensation+200)` instead. `nudgeMs` adds a manual offset.

## Notes

- PLAY is confirmed only by the `SCHEDULED_ACTION` echo — rejections are
  silent server-side. Watch `Logs → plugin-fpp-plugin-FalconBeat.log`.
- BeatSync room→source bindings are in-memory; after a server restart the
  daemon re-registers pushed files on reconnect.
- The admin WS, `/upload/complete`, and ingest are LAN-only by design; only
  browsers reach the server through the public ingress.
- Secrets (creator secret, ingest token, admin secret) are stored in
  `plugindata/fpp-plugin-FalconBeat/secrets` mode `0600` — outside `config/`,
  so backups and crash bundles never carry them.

## Before publishing

- `pluginInfo.json` points at `github.com/GraphicHealer/fpp-plugin-FalconBeat`
  — the GitHub repo must be named `fpp-plugin-FalconBeat` to match `repoName`.
