# fpp-plugin-FalconBeat

FPP (Falcon Player) plugin that plays your light show's audio on visitors'
devices, in sync with the sequence, through a self-hosted
[FalconBeat](https://github.com/GraphicHealer/FalconBeat) server.

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

Content Setup &rarr; FalconBeat: server WS URL, room ID, creator secret,
ingest URL + token, and the public media URL devices fetch audio from.
Status &rarr; FalconBeat Status shows the live connection state.

## Notes

- Secrets are stored in `plugindata/fpp-plugin-FalconBeat/secrets` mode
  `0600` — never in `config/`, so backups and crash bundles don't carry
  them.
- Logs: File Manager &rarr; Logs &rarr; `plugin-fpp-plugin-FalconBeat.log`.
- The admin socket, `/upload/complete`, and ingest are LAN-only by design;
  only browsers reach the server through public ingress.
