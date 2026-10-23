# fpp-plugin-FalconBeat

A self-hosted way to put your light show's audio on your visitors' devices:
they open your listener page, press play, and hear the soundtrack in sync
with the sequence — served entirely by infrastructure you control, through
the [FalconBeat](https://github.com/GraphicHealer/FalconBeat) stack.

## How it works

- fppd calls `callbacks.py` on media/playlist events; it forwards them to a
  background daemon (`falconbeatd.py`) and exits.
- The daemon holds the server's admin socket open for the playlist and
  keeps a rolling two-track window: only the current and next tracks are
  uploaded to ingest and registered on the room, so devices download each
  track during the previous song instead of the whole show at once.
- Each track start is gated until devices confirm the audio loaded; FPP and
  the devices then start at the same scheduled instant. Pauses and stops
  send PAUSE.
- Played or skipped tracks are evicted from the room and deleted from
  ingest. `nowplaying.json` (heartbeated while playing) drives the listener
  page's title/artist and off-air banner; the status page shows the live
  listener count.

## Configure

Content Setup &rarr; FalconBeat: the server's LAN address (one published
port carries everything) and the shared server token. Status &rarr;
FalconBeat Status shows the live connection state.

## Notes

- Secrets are stored in `plugindata/fpp-plugin-FalconBeat/secrets` mode
  `0600` — never in `config/`, so backups and crash bundles don't carry
  them. The token field on the settings page renders blank even when a
  value is saved.
- Logs: File Manager &rarr; Logs &rarr; `plugin-fpp-plugin-FalconBeat.log`.
- The admin socket, `/upload/complete`, and ingest are LAN-only by design;
  only browsers reach the server through public ingress.
