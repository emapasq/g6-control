# Zoom G6 — known USB MIDI commands

Unofficial documentation, not affiliated with or endorsed by Zoom Corporation.
Everything below was tested on a G6 running **firmware 2.00**, the version installed on the development unit. Other firmware versions may behave
differently: test carefully, and never send anything that is not in the **Verified** sections.

Test frames for every format are in [`test-vectors.json`](test-vectors.json).

## Conventions

- USB MIDI port name: `ZOOM G6` (input and output).
- SysEx frame: `F0 52 00 6E <body> F7` — `52` Zoom, `00` device, `6E` G6. Bodies below omit header and `F7`.
- Generic OK reply: `00 00`.
- **val5**: an integer as 5 bytes of 7 bits, least significant first (`90` → `5A 00 00 00 00`, `153` → `19 01 00 00 00`).
- **Packed data**: groups of 8 bytes; the first byte holds the high bit of the next 7 (bit 6 → first byte).
- **Offsets** in reply tables are zero-based on the **complete** frame, `F0` included.
- Slot `N` (1–240) ↔ bank `BB = (N-1) / 4`, position `PP = (N-1) % 4`.

## Modes

| Body | Effect |
|---|---|
| `52` → `00 00` | enter **PC Mode** (display shows "PC Mode"); required for stored patch reads/writes |
| `53` | leave PC Mode; **live commands are ignored while in PC Mode** |
| `50` → `00 00` | editor mode, used before both session types |

**Live session setup** (used before live commands): `50` → `53` → `64 13` → host acknowledges with `00 00`.

## Verified — patches

| Operation | Send | Reply / notes |
|---|---|---|
| Active slot | `33` | CC0 = 0, CC32 = bank, Program Change = position |
| Active patch dump | `64 13`, then `00 00` | `64 12 01 28 06 <packed PTCF> <5 bytes>`; name = unpacked bytes 26–35 |
| Select patch | CC0 = 0, CC32 = bank, Program Change = position (channel 1) | standard MIDI |
| Read stored patch (PC Mode) | `46 00 00 BB 00 PP 00` | `45 00 00 BB 00 PP 00 28 06 <packed PTCF> <5 bytes>`; `Empty` = free slot |
| Write stored patch (PC Mode) | `56` → `45 00 00 BB 00 PP 00 28 06 <packed data> <CRC>` → `57` | OK after each step; CRC = `crc32(808 bytes) ^ 0xFFFFFFFF` as 5 × 7 bits, LSB first. **Overwrites the slot.** |

The PTCF block is the `.zptc` file format: bytes 0–3 `PTCF`, 4–7 length (little endian), name at 26–35.
For writing, it is padded with zeros to 808 bytes.

## Verified — live control (outside PC Mode)

| Operation | Send | Reply / notes |
|---|---|---|
| Select block | `64 20 00 64 01 BB 00 00 00 00` | before every block change |
| Effect on/off (toggle) | `64 20 00 5A 00 01 00 00 00 00` then `64 20 00 5A 00 00 00 00 00 00` | press/release; acts on the selected block |
| Effect parameter | `64 20 00 BB PP <val5>` | `PP` ≥ 2; raw value (not always equal to the display) |
| Change confirmation | — | `64 20 01 BB PP <val5>` |
| Display | `64 02 FIRST LAST 00` | `64 01 …`: 14-byte entries (block, parameter, type 0 = value / 1 = label, `00`, 10 ASCII chars) |
| Tuner on / off | `64 0B` / `64 0C` | distinct commands, not a toggle |

Amp models use two blocks: the second half's parameters are set on the **first** block with parameter number + 4.

## Verified — effect list (PC Mode)

The list of installed effect modules and impulse responses, with the raw range of each parameter.
Guitar Lab reads it in its Effect tab; the sequence below was observed with a real G6.

| Operation | Send | Reply / notes |
|---|---|---|
| Start the list | `64 04` | `00 00`; always start here: the list is stateful |
| Next record | `64 05` | one `64 06` record per module, then `64 07` after the last one |

Send one `64 05` at a time and wait for its reply; do not interleave other requests until `64 07`.

**Effect record** (`64 06`, `27 + 11 × N` bytes):

| Offset | Field | Encoding |
|---|---|---|
| 6–18 | module file name (e.g. `TS_DRIVE.ZD2`) | ASCII, NUL padded |
| 36 | N: parameter count, including on/off and the effect type | 1 byte |
| 37… | N − 1 records of 11 bytes: parameter 0 (on/off), then 2…N−1 (parameter 1, the type, is omitted) | see below |

Each 11-byte record starts with the **maximum** and the **default** raw value, each 2 × 7 bits LSB first.
The remaining 7 bytes are not documented. Raw values start at 0: a parameter shown as 1–4000 is sent as 0–3999.
Impulse responses appear as `IRnn.ZD2`. The display shows the effect name, not the file name: the MCP server maps one
to the other with the module table of [zoom-zt2](https://github.com/mungewell/zoom-zt2).

## Verified — rhythm (drum machine)

| Operation | Send | Reply / notes |
|---|---|---|
| Start | `64 20 00 5B 00 01 00 00 00 00` | works in any play mode |
| Stop | `64 20 00 5B 01 01 00 00 00 00` | |
| Read state | `64 0A` | `64 09`, 61-byte frame (see below) |
| Set BPM | `64 20 00 64 02 <val5>` | 40–250; **global** tempo (shared with effects and looper) |
| Set pattern | `64 20 00 64 5A <val5>` | 0–67 = manual pattern No. − 1 (32 = No. 33 IndiePop) |
| Set volume | `64 20 00 64 5B <val5>` | 0–100 |
| Set COUNT | `64 20 00 64 5C <val5>` | 0 = off, 1 = on (looper precount) |
| Setter confirmation | — | `64 20 01 64 PP <val5>` with the value applied |

**Rhythm state reply** (`64 09`, 61 bytes):

| Offset | Field | Encoding |
|---|---|---|
| 6–7 | BPM | 2 × 7 bits, LSB first |
| 41 | pattern | 1 byte |
| 42 | volume | 1 byte |
| 43 | COUNT | 1 byte |
| 45 | Looper STOP MODE | 0 INSTANT, 1 FINISH, 2 FADE OUT — **verified** (see *Looper settings*) |
| 46 | Looper volume | 0–100 — **verified** (see *Looper settings*) |
| 48 | Looper POSITION | 0 PRE-EFFECTS, 1 POST-EFFECTS — **verified** (see *Looper settings*) |
| 49 | Looper LOOP MODE (preference) | 0 MONO **read on the device**; 1 STEREO **not verified**; other values not qualified |

In every captured frame byte 49 is 0, so values other than MONO cannot be told apart from neighbouring bytes yet.
LOOP MODE is the saved MONO/STEREO preference: with an SD card inserted the G6 records in STEREO and the setting
cannot be changed (owner's manual), whatever byte 49 says.
Other bytes are not documented. **Playback state (playing/stopped) is not
available**: no read command for it is known, so clients must show it as unknown. These are settings, not transport.

## Verified — Looper settings

| Operation | Send | Reply / notes |
|---|---|---|
| Set STOP MODE | `64 20 00 64 5E <val5>`, values **0, 1, 2 only** | confirmation `64 20 01 64 5E <val5>`; read back with `64 0A` (byte 45) |
| Set Looper volume | `64 20 00 64 5F <val5>`, 0–100 | confirmation `64 20 01 64 5F <val5>`; read back with `64 0A` (byte 46) |
| Set POSITION | `64 20 00 64 61 <val5>`, values **0, 1 only** | confirmation `64 20 01 64 61 <val5>`; read back with `64 0A` (byte 48). **Stops and resets the Looper** |

Verified on firmware 2.00 (1 November 2026): INSTANT → FINISH → INSTANT and INSTANT → FADE OUT → INSTANT, each with the
confirmation, an independent `64 0A` read-back in which only byte 45 changed, and the display (FINISH, FADE OUT and the
second return to INSTANT confirmed visually). This verifies the **setting**, not the audio behaviour of a stop, nor
persistence after a power cycle (the device may save it). A stop follows the current STOP MODE: it is not forced to
INSTANT.

Looper volume, verified on firmware 2.00 (1 November 2026): byte 46 matched the display at 60 and, after changing it
on the device, at 45, also after a power cycle; the setter to 50 and back to 60 was confirmed by the device, read back
(only byte 46 changed between the two reads) and shown on the display. The value survived a power cycle.

POSITION, verified on firmware 2.00 (1 November 2026): byte 48 read 0 after selecting PRE-EFFECTS on the device; the
setter to 1 and back to 0 was confirmed by the device, read back (only byte 48 changed) and shown on the display. No
other message arrived within 2 s of each setter. Changing POSITION, on the device or over USB, stops and resets the
Looper: a loop or a loaded WAV is dropped from the Looper (the files on the SD card stay). Assume this also happens when
the same value is sent again: read the current value first and never resend it automatically.

Not used by these tools: the LOOP MODE setter (fixed to STEREO with an SD card inserted; changing it resets the Looper
content).

## Verified — SD card and WAV files (read-only)

| Operation | Send | Reply |
|---|---|---|
| SD space | `64 3C` | `64 3D`, 27 bytes: total at offsets 6–15, free at 16–25, each 10 × 7 bits LSB first (bytes) |
| WAV count | `64 35` | `64 36`, 9 bytes: count at offsets 6–7, 2 × 7 bits LSB first |
| WAV list | `64 37 <start> <end>` (each 2 × 7 bits, LSB first, inclusive, at most 50 records) | `64 38`: record count at offset 6, then 64-byte NUL-padded names from offset 7 |

The count and the list come from a cache on the device: when it is refreshed is not known, and indices may change
between enumerations. Never use a WAV index as a permanent identifier; read the count before listing.

## Verified — WAV load into the Looper (changes the Looper session)

| Operation | Send | Reply / notes |
|---|---|---|
| Load WAV | `64 39 LL HH` (index 0-based, `LL` = index & 7F, `HH` = index >> 7; at most 256 files) | generic reply `00 RR`; `64 3B` with the loaded file name; sometimes `64 20` parameter updates |

Verified twice on firmware 2.00 (1 November 2026), each time with the Looper stopped and empty beforehand and the result
confirmed on the device display: index 44 (`F0 52 00 6E 64 39 2C 00 F7`) loaded `LOOPER_043.WAV`. The second time the
index was resolved by name by the MCP server; the G6 replied `00 00`, announced `LOOPER_043.WAV` in `64 3B`, sent no
`64 20` updates, and left the BPM unchanged. A load from the web app's *Load* button was then also confirmed on the
device display.

- **Not a read.** The load replaces the Looper session: work files are rebuilt, data the Looper held may be finalised
  or lost, and the global BPM may be taken from the file's metadata. Load only with the Looper stopped and holding
  nothing to keep, and with no other program sending at the same time.
- **The index is not an identifier.** The device loads from its own scan of the looper folder, not from the cache read
  by `64 35` / `64 37`: if the cache is stale, the same index can be a different file. Resolve the index by name from a
  fresh list just before loading, and compare the name announced in `64 3B` with the one requested.
- **The reply is not proof.** `00 00` means the device completed the command, not that the copy is correct; the reply
  has no opcode or index. Other values of `RR` are not documented. Use 44.1 kHz, 16-bit stereo WAV files, and check the
  Looper screen after every load.
- Send once and never retry: a missing reply does not mean the load did not happen.
- **Replies cannot be correlated.** `00 RR` is the same generic reply used by other requests, with no opcode, index
  or transaction id, and no maximum duration of a load is known. A reply that arrives after the host stopped listening
  can be received during the next request. Waiting longer or discarding pending input does not rule this out. Clients
  should treat a missing reply, or more than one reply while listening, as an **uncertain outcome**, block further loads
  until a person has checked the Looper screen, and never present `RR` as the certain result of the current load.
- `64 3B` may not arrive in every screen or session: its absence is reported, not treated as a failure.
- Nothing here plays, stops, records, overdubs, clears or deletes.

## Verified once — Looper play and stop (gated)

| Gesture | Send | Observation (firmware 2.00, 1 November 2026) |
|---|---|---|
| Play | `64 20 00 5C 00 01 00 00 00 00` | sent once with `LOOPER_044.WAV` loaded and stopped, Rhythm stopped, STOP MODE *Instant*: playback started (display and listening) |
| Stop | `64 20 00 5C 01 01 00 00 00 00` | sent once while `LOOPER_044.WAV` was playing: playback stopped (display and listening) |

No reply was waited for; none is documented. Block `5C`, parameter 0 is the Looper's **main foot-switch gesture**:
with a loaded, stopped loop it plays, but with an empty Looper it can start a **recording** and while playing an
**overdub**. No reliable way to read the Looper state over USB is known. Clients must therefore gate it:

1. a WAV load sent by the same client, whose outcome was not uncertain;
2. a manual confirmation, checked on the device display, that the WAV is loaded and the Looper stopped;
3. play sent **once**, the permission consumed before sending; a new play needs a new confirmation;
4. stop available only after that play, sent once; if it does not stop, stop from the foot switch.

Stop with other STOP MODE settings, and either gesture in other Looper states, were not tested.
The foot switches can change the Looper state at any time: `64 3B` notifications report Looper events but not which
one. The web app withdraws the play permission when one arrives; the MCP server cannot, as it does not listen between
calls.

## Notifications (listen only — never send)

| Message | Meaning |
|---|---|
| `64 26 00 00 BB 00 PP 00`, then CC0/CC32/Program Change | patch changed from the footswitches |
| `64 20 00 BB 00 01 00 00 00 00` | an effect was switched on/off from the device (value is always 1: read the display for the state) |
| `64 3B <64 ASCII bytes>` | looper event, with the current file name |
| NRPN CC98 (note 0–11, 12 = no signal) / CC99 (deviation, 64 = in tune) | tuner data while the tuner is on |

Changes made on the device itself (rhythm start/stop, tempo, pattern, volume) were **not** observed to produce notifications:
re-read the state (`64 0A`) to stay in sync.

## Dangerous

- `64 20 00 BB 00 …` with `BB` = an effect block (0–8): this is the device's **notification** form. Sending it changed the
  audio without updating the display and then froze the G6 (power cycle needed).
- Stored patch writes overwrite the slot without asking: back up first.
- A WAV load replaces the Looper session: see *WAV load* above.
- If the device stops responding: power it off and on.

## Not documented

Any other command family or message is intentionally left out. Do not send messages that are not listed above.
