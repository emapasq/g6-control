# G6 Control — MCP server

An [MCP](https://modelcontextprotocol.io) server that lets an AI assistant (Claude, Codex, …) **control a Zoom G6**
connected to the computer over USB: read and write stored patches, edit the active patch live, run the drum machine,
switch the tuner, read SD card information and load WAV files into the Looper. Together with an assistant that knows the `.zptc` format, it can also
generate new patches and load them onto the device.

> **AI-generated code**: written by AI assistants and tested on a real G6, without a line-by-line human review — see
> the [main README](../README.md#ai-generated-code).
>
> **Unofficial project**, not affiliated with Zoom Corporation. Verified on firmware 2.00 only (see *Compatibility*). See the safety notes in
> [`../protocol/COMMANDS.md`](../protocol/COMMANDS.md).

## Compatibility

Verified on a G6 with **firmware 2.00**, the version on the development unit. Not tested with other versions.

`effect_set_param` identifies the effect of a block by the **effect id stored in the active patch**, then looks up
its module file in `g6_mcp/data/effect_files.json` and the parameter's maximum in the effect list read from the G6.
The table comes from zoom-zt2's module list, whose firmware version is not stated. It was checked on firmware 2.00:
all its modules are installed, the ids of every effect used in 122 stored patches are in it, and the six modules it
lacked (**OverDrive, BitCrush, DynaDelay, FxLoop, Send, Return**) were added from the device; the G6 ids of FxLoop,
Send and Return (`0x1a000020/23/26`) come from patches saved on it. Impulse responses (`0x1b…`) are not mapped: their
values are refused. Anything the table does not know is refused and nothing is sent.
The parameter ranges themselves are always read from the connected G6, so they match its firmware.

## Install

Python 3.10+ on the Mac (or PC) the G6 is connected to:

```bash
cd mcp-server
python3 -m venv .venv
.venv/bin/pip install -e .
```

Register it in your assistant. **Claude Desktop** (`~/Library/Application Support/Claude/claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "zoom-g6": {
      "command": "/path/to/g6-control/mcp-server/.venv/bin/g6-mcp",
      "env": { "G6_BACKUP_DIR": "/path/to/your/g6-backups" }
    }
  }
}
```

**Codex** (`~/.codex/config.toml`):

```toml
[mcp_servers.zoom-g6]
command = "/path/to/g6-control/mcp-server/.venv/bin/g6-mcp"
env = { G6_BACKUP_DIR = "/path/to/your/g6-backups" }
```

`G6_BACKUP_DIR` is where `patch_write` saves a patch before overwriting it (default `~/.g6-control/backups`).

## Tools

| Area | Tool | What it does |
|---|---|---|
| Stored patches | `patch_list()` | names of the 240 slots and the free ones (read-only, PC Mode for a few seconds) |
| | `patch_read(slot)` | a slot as a `.zptc` file in base64, the same format Guitar Lab exports |
| | `patch_write(zptc_base64, slot, overwrite=False)` | writes a `.zptc`; refuses an occupied slot unless `overwrite=True`, then backs the old one up first; reads it back and compares |
| | `patch_format_guide()` | how to build a `.zptc`: layout, value encoding, foot switches, Python helpers and the effect table (ids, processing load, parameters); sends nothing |
| Active patch | `patch_status()` | slot, name, and every block with ON/OFF and its numbered parameters as on the display |
| | `patch_select(slot)`, `patch_next()`, `patch_previous()` | change patch |
| | `effect_set_enabled(block, enabled)` | switch a block on/off (acts only if needed) |
| | `effect_set_param(block, param, value)` | set a raw parameter value, checked against the maximum the G6 reports for that effect module |
| | `effects_list(search="", include_ir=False)` | installed effects with raw maximum/default of each parameter |
| Rhythm | `rhythm_status()` | BPM, pattern, volume, COUNT (playing/stopped is not readable) |
| | `rhythm_start()`, `rhythm_stop()` | drum machine transport |
| | `rhythm_set_bpm(bpm)`, `rhythm_set_pattern(number)`, `rhythm_set_volume(volume)`, `rhythm_set_count(enabled)` | settings; the pattern uses the owner's manual numbering (1–68) |
| Tuner | `tuner_on()`, `tuner_off()` | |
| SD card | `storage_sd_space()`, `storage_wav_count()`, `storage_wav_list(start=0, end=None)` | read-only; the list needs a count first |
| | `storage_wav_load(name, looper_stopped_confirmed=False)` | loads a WAV into the Looper by file name; see *WAV load* below |
| | `storage_wav_load_clear_uncertain(looper_checked_confirmed=False)` | allows loads again after an uncertain outcome; sends nothing |
| Looper | `looper_arm_play(wav_loaded_and_looper_stopped_confirmed=False)` | allows one play after a confirmation on the display; sends nothing |
| | `looper_play()`, `looper_stop()` | one play (only when armed), one stop (only after that play); see *Looper play and stop* below |
| | `looper_settings()` | volume, STOP MODE, POSITION and LOOP MODE (read-only; settings, not the transport state; LOOP MODE is the saved preference: with an SD card the Looper is always STEREO) |
| | `looper_set_volume(volume)` | Looper volume 0–100; sent once, then read back |
| | `looper_set_stop_mode(mode)` | 0 INSTANT, 1 FINISH, 2 FADE OUT; sent once, then read back; refused while a play sent by this server is not stopped |
| | `looper_set_position(position, looper_reset_confirmed=False)` | 0 PRE-EFFECTS, 1 POST-EFFECTS. **Stops and resets the Looper**: needs the user's confirmation; nothing is sent if the value already matches; sent once, then read back; refused while a play sent by this server is not stopped |

Live changes (`effect_*`, `rhythm_*`) are **not saved** to the patch.

## Safety

- Only messages listed as verified in `COMMANDS.md` are sent: `g6_mcp/protocol.py` enforces an allowlist, and a patch
  write is only sent with a valid PTCF block and CRC.
- Nothing is resent automatically; a missing confirmation is reported as such.
- An occupied slot is never overwritten without `overwrite=True`, and never without a backup.
- One tool call at a time per server. Other programs (the web app, Guitar Lab) can stay open, but avoid sending from two
  of them at the same instant: replies have no transaction id.

## WAV load

`storage_wav_load` replaces the Looper session (data it held may be
finalised or lost) and the G6 may take the global BPM from the file. It never plays, stops, records or deletes.

1. It refuses to send anything unless `looper_stopped_confirmed=True`: the assistant must ask the user first.
2. It reads the WAV count and the whole list again, and resolves the name (case-insensitive). A missing name or one
   that matches more than one entry is refused.
3. It re-reads that single entry, then sends `64 39` once, with no retry.
4. It reports what arrived while listening (up to 10 s, then 1.5 s after a reply): generic replies `00 RR`, the file
   name the G6 announces in `64 3B` (a different name means the device's own folder scan did not match the list; no
   name is reported as such) and the BPM before and after. A single `00 RR` is reported as a reply that cannot be tied
   to this load with certainty, never as a definitive success or refusal. Check the Looper screen after each load.
5. **Uncertain outcome**: no reply in time, or more than one reply. Further loads are refused until the user has
   checked the Looper screen and `storage_wav_load_clear_uncertain(looper_checked_confirmed=True)` is called (it sends
   nothing). The state is written to `G6_BACKUP_DIR/.wav-load-uncertain.json` before the load is sent, so it also
   survives a crash or a restart of the server.

Limits that remain: the reply has no transaction id, so a reply arriving after the listening window (even after the
state was cleared) can still be received during a later request and is then counted there; the protection covers
this server only, not the web app or other programs sending at the same time.

## Looper play and stop

Block `5C`, parameter 0 is the Looper's main foot-switch gesture: with a loaded, stopped loop it plays, but with an
empty Looper it can record and while playing it can overdub. The server therefore follows a fixed flow:

1. `storage_wav_load` by this server, with an outcome that is not uncertain.
2. `looper_arm_play(wav_loaded_and_looper_stopped_confirmed=True)`, only after the user has checked the display
   (WAV loaded, Looper stopped). It sends nothing.
3. `looper_play()`: the permission is used up and written to disk before the port is opened; the play is sent once.
   A second play is refused until a new `looper_arm_play`.
4. `looper_stop()`: available only after that play, sent once. If the loop keeps playing, use the foot switch.

A new WAV load or a POSITION change withdraws the permission (POSITION also drops the loaded WAV). The state (`G6_BACKUP_DIR/.looper-state.json`) survives restarts; the play
permission is never re-created by a restart. Limits: the server does not listen between calls, so it cannot notice a
foot switch pressed after the confirmation; the confirmation is the user's, and the device state cannot be read over
USB. Play and stop were verified on firmware 2.00 with STOP MODE *Instant* only: a stop follows the current STOP MODE
(`looper_settings`), and the server never changes it on its own.

## Using it to generate patches

Ask the assistant for a patch in plain words ("a Gilmour-style lead for my Strat, with delay on foot switch 2"). The
server moves `.zptc` files; the assistant builds them, following the guide returned by `patch_format_guide()` (the same
text is in [`g6_mcp/data/patch_format.md`](g6_mcp/data/patch_format.md)). The usual flow:

1. Start from `patch_list()` and pick a **free slot**; overwrite only when the user asks.
2. Read the guide, and use an existing patch (`patch_read`) as a template for the parts of the file that are not
   decoded.
3. A patch has 9 blocks; amp models take two. The device also limits the total processing load of a patch (the guide
   lists the load of each effect): if a block does not load, the patch is over the limit.
4. Values in the file and in `effect_set_param` are raw: they start at 0 (a parameter shown as 1–4000 is 0–3999), and
   lists are indices. `effects_list` gives the maximum of every parameter.
5. After `patch_write`, `patch_select(slot)` and `patch_status()` show what the device actually loaded. Fine-tune live
   with `effect_set_param`, then write the final version.

## Tests

```bash
cd mcp-server
python3 -m unittest discover -s tests   # no device needed; the registration test needs `mcp`
```

The protocol tests share [`../protocol/test-vectors.json`](../protocol/test-vectors.json) with the web app.
The tool tests run against a simulated G6: no MIDI port is opened.

## Credits

The effect module table in `g6_mcp/data/effect_files.json` comes from
[zoom-zt2](https://github.com/mungewell/zoom-zt2) by mungewell (MIT License), completed with six modules read from a
G6 running firmware 2.00.
