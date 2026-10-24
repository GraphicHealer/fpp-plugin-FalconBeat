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
  track during the previous song instead of the whole show at once. On a
  repeating playlist the window wraps — while the last song plays, the
  first song is already back in the room as "next".
- Playback events are fire-and-forget — FPP's timing is never held up.
  Near each track's end the daemon pre-schedules the next song's play on
  the server's synchronized clock, so every song starts at a pinned
  instant with the same bias — consistent song after song (an older
  server build just ignores the pinned time and each play lands at the
  right offset a schedule-delay late). A self-check after each start
  corrects any large miss once. Video entries upload their extracted
  soundtrack, not the whole video. Pauses and stops send PAUSE.
- Played or skipped tracks are evicted from the room and deleted from
  ingest. `nowplaying.json` (heartbeated while playing) drives the listener
  page's title/artist and off-air banner; the status page shows the live
  listener count.

## Configure

Input/Output Setup &rarr; FalconBeat. Enable it, then three fields:

- **Server (LAN)** — `falconbeat.local` or `host:port`; one published port
  carries everything.
- **Server Token** — the `FALCONBEAT_TOKEN` from your server.
- **Sync Offset** — shifts where devices join the track. Raise it if the
  audio trails the lights, lower it if it runs ahead.

An Advanced group appears when you raise FPP's UI level.
Status &rarr; FalconBeat Status shows the live connection state.

Want a gap between songs so devices can settle? Add a Pause entry to the
playlist — FPP handles it natively.

**Show Info** (same page): name, tagline, hosted-by, schedule, location,
description, links (`Label = URL`, one per line), and your public listener
URL — the daemon pushes them to the server's `show.json` on every change and
the listener page renders them in its Show Info sheet. Name/tagline default
to FPP's HostName/HostDescription. The **Signage QR** at the bottom of the
page encodes the public listener URL for your yard sign.

## Notes

- Secrets are stored in `plugindata/fpp-plugin-FalconBeat/secrets` mode
  `0600` — never in `config/`, so backups and crash bundles don't carry
  them. The token field on the settings page renders blank even when a
  value is saved.
- Logs: File Manager &rarr; Logs &rarr; `plugin-fpp-plugin-FalconBeat.log`.
- The admin socket, `/upload/complete`, and ingest are LAN-only by design;
  only browsers reach the server through public ingress.
