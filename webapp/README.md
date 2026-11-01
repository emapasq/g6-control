# G6 Control — web app

> **AI-generated code**: written by AI assistants and tested on a real G6, without a line-by-line human review — see
> the [main README](../README.md#ai-generated-code).

Control panel for the Zoom G6 in Chrome on the Mac connected to it over USB.

## Run

```bash
docker compose up -d
```

Open **http://localhost:8616** in **Chrome** (Safari has no Web MIDI), click **Connect** and allow MIDI access
with SysEx. Afterwards the page connects by itself on load. Files in `public/` are served live: reload to see edits.

The port is published on `127.0.0.1` only: Web MIDI requires a secure context (localhost or HTTPS), and the G6 must be
connected to this Mac anyway.

## Features

| Area | What it does |
|---|---|
| Patch | active slot and name, previous/next, select by number or from the list; *Load patch list* reads the 240 names (PC Mode for a few seconds, read-only) |
| Rhythm | BPM (type + Enter, ±1/±5), pattern, volume, count, start/stop; state read from the G6 on connect, when the page regains focus, after each change and with *Refresh* |
| SD card | free/total space and the list of WAV files, on demand (*Read SD card*), from the device's file cache |
| Looper | after a load from this page: tick *On the display: the WAV is loaded and the Looper is stopped. Allow one Play.*, then **Play** (sent once, then disabled until you confirm again) and **Stop** (only after that Play, sent once). A Looper event from the device (e.g. a foot switch) withdraws the permission; a reload never restores it. No keyboard shortcuts. **Looper volume** (slider, 0–100, one setter after a short pause, then read back) and **STOP MODE** (Instant / Finish / Fade out) can be changed: one setter, then read back; not while a play sent by this page is running. **POSITION** (Pre-effects / Post-effects) can be changed only after ticking *Changing POSITION stops and resets the Looper*: the current value is read first (nothing is sent if it already matches), then one setter and a read back; the Looper is reset, so the loaded WAV and Play are dropped. LOOP MODE is not shown: with an SD card the Looper is always stereo. These settings are read with the rhythm state; they are settings, not the transport state |
| WAV load | *Load* next to a file puts it into the Looper. Enabled only after ticking *The Looper is stopped and holds nothing I need to keep* (cleared after every load). The page re-reads the list, resolves the name (missing or ambiguous: nothing sent), sends one `64 39`, and reports the reply, the file name announced by the G6 and any BPM change. A single `00 RR` reply is shown as not tied to the load with certainty; a missing `64 3B` is shown as *No file name announced*. With no reply in time or more than one reply, the outcome is **uncertain**: loads stay blocked (also after a page reload) until you press *I checked the Looper screen: allow loads again*, which sends nothing. It never plays, records or deletes; check the Looper screen after each load |
| Sync | footswitch patch changes are detected automatically; rhythm changes made on the device are picked up on the next read |

Playback (playing/stopped) cannot be read from the device: the page shows it as *unknown* and only reports the last command it sent.

Keyboard: ← → patch · S start · X/Esc stop · ↑ ↓ BPM ±1 (Shift ±5) · R refresh rhythm.

## Structure

| File | Role |
|---|---|
| `public/js/g6-protocol.js` | builds/parses messages; allowlist that blocks anything not verified |
| `public/js/midi.js` | Web MIDI transport: ports, request/reply, one conversation at a time |
| `public/js/actions.js` | named actions (`rhythm.setBpm`, `patch.next`, …): the only entry point to the device |
| `public/js/ui.js` | page: controls call actions, rendering from the state |
| `public/js/inputs/` | place for future input sources (e.g. a MIDI foot controller) |
| `tests/` | codec tests against `../protocol/test-vectors.json`; WAV load and Looper play/stop against a simulated G6 (`fake-g6.js`) (`npm test`) |

## Coexistence with other software

Other programs (e.g. Zoom Guitar Lab or an MCP server) can use the G6 at the same time, but avoid sending commands from
two of them at the same instant: replies have no transaction id and could be attributed to the wrong request.
