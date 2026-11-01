# Zoom G6 patch format (.zptc) — guide for building patches

Firmware 2.00. This is the format of the `PTCF` block that `patch_read` returns and `patch_write` accepts (the same
format Guitar Lab exports as `.zptc`). It was worked out by comparing patch files saved on a G6: the helpers at the
end rebuild all 118 patches stored on the development unit byte for byte (description texts left empty), and patches
built with these rules were written to the device and played. What is marked *unknown* has not been decoded: copy it
from an existing patch.

## Recommended workflow

1. `patch_list()`: pick a **free slot**. Overwrite only when the user asks.
2. `patch_read(slot)` of an existing patch, ideally one using similar effects: it is the **template** for the header
   and the `PRM2` block.
3. Choose the chain (at most 9 blocks; amps and some large effects take 2; total processing load at most 100%, see the
   table below), then build the file as described here.
4. `patch_write(zptc_base64, slot)`, then `patch_select(slot)` and `patch_status()` to see what the device loaded.
   Fine-tune with `effect_set_param`, and write the final version when the user is happy.

## Layout (integers little-endian)

| Offset | Size | Content |
|---|---|---|
| 0x00 | 4 | `PTCF` |
| 0x04 | 4 | file size in bytes (uint32) |
| 0x08 | 4 | `02 00 00 00` |
| 0x0C | 4 | number of chain entries `n` (uint32) |
| 0x10 | 4 | `00 10 00 00` |
| 0x14 | 6 | zeros |
| 0x1A | 10 | patch name, ASCII, padded with spaces |
| 0x24 | 4·n | effect id of each chain entry, in chain order (uint32) |
| 0x24+4n | 8 + len | `TXJ1` + uint32 length + Japanese description text (usually empty: length 0) |
| … | 8 + len | `TXE1` + uint32 length + English description text (usually empty: length 0) |
| … | 8 + 24·n | `EDTB` + uint32 length (24·n) + one 24-byte entry per chain entry |
| … | 8 + 32 | `PRM2` + uint32 length (32) + patch settings |

File size = 100 + 28·n + the two text lengths (100 + 28·n with empty texts). Find the four tags by searching for them
rather than using fixed offsets: they move with `n`.

## Chain entries

- Entry k is block k on the display. An **amp takes two entries**: its id, then `0x00000001`. Some other large effects
  also take two blocks (see *Blocks* in the table).
- An empty entry has id `0x00000000`.
- The G6 also limits the **processing load**: the sum of the *Load %* values below must not exceed 100. If a block
  does not load on the device, the patch is over the limit.

## EDTB entry (24 bytes = one little-endian 192-bit integer)

| Bits | Content |
|---|---|
| 0 | on (1) / off (0) |
| 1–29 | effect id (29 bits: the same value as in the chain list) |
| 30 onward | parameters, in the order of the table below: parameters 1–5 take **12 bits** each, parameters 6 and later **8 bits** each |

- The second entry of an amp is `03 00 … 00` (on, id 1). An empty entry is `01 00 … 00`.
- Values are **raw**: stored = displayed value − the range minimum. OFF/ON = 0/1. Lists are indices from 0. Examples:
  Time 1–4000 at 450 ms is stored as 449; Sense −10–10 at −5 is stored as 5; Hall PreD 30 is stored as 29.
- Exception: PolyShift *Shift* is not contiguous (−12 is stored as 1, −5 as 8).
- Hidden parameters: some amps store one more parameter than the effect list shows (MS 1959 and BG MK1: value 2).
  Copy it from a patch that uses the same amp.
- `effects_list()` reads the raw maximum and default of every parameter from the connected G6: use it to check a value.

### Tempo-synced times (delays)

After the last value in milliseconds, time parameters continue with note values. With a maximum of M ms (stored M−1),
stored M is a 1/16 note, then in order: M+1 1/8 triplet, M+2 dotted 1/16, M+3 1/8, M+4 1/4 triplet, M+5 dotted 1/8,
M+6 1/4, M+7 dotted 1/4, and M+6+k = k quarter notes (k ≥ 2). Verified on DynaDelay (M = 2000, up to 8 quarters) and
AnalogDly (M = 4000, up to 16 quarters).

## PRM2 (32 bytes)

- Bytes 28–31 (uint32): **foot switch assignments**. Nibble 0: keep 0. Nibbles 1, 2, 3, 4 = foot switch 1, 2, 3, 4:
  the chain position (from 0, counting the second entry of an amp) of the block it switches; `F` = none.
  Example: `0x000F7620` = FS1 → position 2, FS2 → position 6, FS3 → position 7, FS4 → none.
- The other bytes (patch level, tempo and other settings) are *unknown*: copy them from the template patch. They also
  carry the template's settings, so always set the foot switches explicitly.

## Python helpers

```python
ENTRY = 24

def widths(count):
    return [12] * min(count, 5) + [8] * max(count - 5, 0)

def edtb_entry(effect_id, on, params):
    """params: raw values in table order."""
    value = (1 if on else 0) | (effect_id << 1)
    pos = 30
    for raw, width in zip(params, widths(len(params))):
        if not 0 <= raw < 1 << width:
            raise ValueError(f'value {raw} does not fit in {width} bits')
        value |= raw << pos
        pos += width
    return value.to_bytes(ENTRY, 'little')

def build_patch(template, name, chain, footswitches):
    """template: bytes of an existing patch (header and PRM2 are copied).
    chain: list of (effect_id, on, params); an amp is followed by (1, True, []).
    footswitches: up to 4 chain positions (0-based) or None."""
    import struct
    n = len(chain)
    prm2 = bytearray(template[template.find(b'PRM2') + 8:][:32])
    fs = 0
    for k in range(4):
        p = footswitches[k] if k < len(footswitches) and footswitches[k] is not None else 0xF
        fs |= p << (4 * (k + 1))
    prm2[28:32] = fs.to_bytes(4, 'little')
    body = bytearray(b'PTCF') + bytes(4) + struct.pack('<II', 2, n) + bytes(template[0x10:0x1A])
    body += name.ljust(10)[:10].encode('ascii')
    body += b''.join(struct.pack('<I', effect_id) for effect_id, _, _ in chain)
    body += b'TXJ1' + bytes(4) + b'TXE1' + bytes(4)
    body += b'EDTB' + struct.pack('<I', ENTRY * n) + b''.join(edtb_entry(*e) for e in chain)
    body += b'PRM2' + struct.pack('<I', 32) + prm2
    body[4:8] = struct.pack('<I', len(body))
    return bytes(body)
```

## Effects (firmware 2.00)

*Load %* is the processing load shown on the G6 screen. *Blocks*: 2 = takes two chain entries; `?` = more than four
parameters, not checked. Parameters are listed in storage order as `name min–max` or `name (choices)`. Impulse
responses (ids `0x1b…`, 27% each) are not listed. Ids come from the zoom-zt2 module list (MIT) and patches saved on a
G6; the module file of each id is in `effect_files.json`.

### Dynamics

| Id | Effect | Load % | Blocks | Parameters |
|---|---|---|---|---|
| 0x01000010 | Comp | 4 | 1 | Sense 0–10 · ATTCK (SLOW, FAST) · Tone 0–10 · VOL 0–100 |
| 0x01000020 | RackComp | 6 | 1 | THRSH 0–50 · Ratio 1–10 · ATTCK 1–10 · VOL 0–100 |
| 0x01000030 | SlowATTCK | 4 | 1 | Time 1–50 · Curve 0–10 · Tone 0–100 · VOL 0–100 |
| 0x01000040 | ZNR | 4 | 1 | DETCT (GTRIN, EFXIN) · Depth 0–100 · THRSH 0–100 · Decay 0–100 |
| 0x01000050 | MuteSW | 6 | 1 | Edge 0–100 · Speed 0–100 · INVRT (NORMAL, INVERT) · ON/OFF (LATCH, UnLATCH, TRGGR) |
| 0x01000060 | GrayComp | 16 | 1 | SUSTN 0–100 · Lo 0–100 · Hi 0–100 · VOL 0–100 |
| 0x01000070 | NoiseGate | 5 | 1 | DETCT (GTRIN, EFXIN) · Depth 0–100 · THRSH 0–100 · Decay 0–100 |
| 0x01000080 | OptComp | 10 | 1 | Drive 0–10 · Lo 0–100 · Hi 0–100 · VOL 0–100 |
| 0x01800010 | BlackOpt | 12 | 1 | Comp 0–100 · Lo 0–100 · Hi 0–100 · VOL 0–100 |
| 0x01800020 | LMT-76 | 11 | 1 | Input 0–80 · Ratio (4:1, 8:1, 12:1, 20:1) · REL 10–70 · Output 0–80 |

### Filter

| Id | Effect | Load % | Blocks | Parameters |
|---|---|---|---|---|
| 0x02000010 | AutoWah | 6 | 1 | Mode (DOWN, UP) · Sense 1–10 · RESO 0–10 · VOL 0–100 |
| 0x02000020 | Resonance | 6 | 1 | Mode (DOWN, UP) · Sense 1–10 · RESO 0–10 · VOL 0–100 |
| 0x02000030 | Cry | 5 | 1 | Range 1–10 · RESO 0–10 · Sense −10–−1, 1–10 · BAL 0–100 |
| 0x02000040 | SeqFLTR | 7 | 1 | Step 2–8 · PTTRN 1–8 · Speed 1–50 · RESO 0–10 |
| 0x02000051 | Gt GEQ | 5 | ? | 160 −12–12 · 400 −12–12 · 800 −12–12 · 3.2k −12–12 · 6.4k −12–12 · 12k −12–12 · VOL 0–100 |
| 0x02000054 | Gt GEQ 7 | 5 | 2 | 100 −12–12 · 200 −12–12 · 400 −12–12 · 800 −12–12 · 1.6k −12–12 · 3.2k −12–12 · 6.4k −12–12 · VOL 0–100 |
| 0x02000061 | St Gt GEQ | 9 | ? | 160 −12–12 · 400 −12–12 · 800 −12–12 · 3.2k −12–12 · 6.4k −12–12 · 12k −12–12 · VOL 0–100 |
| 0x02000070 | ParaEQ | 4 | 1 | FREQ 20 Hz–20 kHz (list) · Q 0.5–16 (list) · Gain −12–12 · VOL 0–100 |
| 0x02000081 | EG FLTR | 9 | ? | FREQ1 0–100 · FREQ2 0–100 · RESO 0–100 · Type (list, HPF2…LPF4) · Speed 0–100 · BAL 0–100 · VOL 0–100 |
| 0x02000090 | RndmFLTR | 7 | 1 | Type (HPF, BPF, LPF) · Speed 1–50 · BAL 0–100 · VOL 0–100 |
| 0x020000a0 | LowPassFL | 7 | 1 | FREQ 0–100 · Sense (list, FST100…SLW100) · RESO (list, 2P-10…4P-10) · BAL 0–100 |
| 0x020000b0 | Exciter | 7 | 1 | Bass 0–100 · Treble 0–100 · VOL 0–100 · ON/OFF (LATCH, UnLATCH) |
| 0x020000c0 | Step | 6 | 1 | Depth 0–100 · Rate 0–50 · RESO 0–10 · Shape 0–10 |
| 0x020000d0 | LFO FLTR | 7 | 1 | Depth 0–100 · Rate 1–50 · RESO 0–10 · Wave (SINE, TRI, SAWUP, SAWDN) |

### Drive

| Id | Effect | Load % | Blocks | Parameters |
|---|---|---|---|---|
| 0x03000010 | TS Drive | 13 | 1 | Gain 0–100 · Boost (OFF, ON) · Tone 0–100 · VOL 0–100 |
| 0x03000020 | EP Stomp | 10 | 1 | Gain 0–100 · Bass −10–10 · Treble −10–10 · VOL 0–100 |
| 0x03000030 | RC Boost | 12 | 1 | Gain 0–100 · Bass 0–100 · Treble 0–100 · VOL 0–100 |
| 0x03000040 | GoldDrive | 17 | 1 | Gain 0–100 · Bass 0–100 · Treble 0–100 · VOL 0–100 |
| 0x03000050 | SweetDrv | 23 | 1 | Gain 0–100 · Tone 0–100 · Focus 0–100 · VOL 0–100 |
| 0x03000060 | DYN Drive | 17 | 1 | Gain 0–100 · Tone 0–100 · Mode (COMBO, STACK) · VOL 0–100 |
| 0x03000070 | RedCrunch | 14 | 1 | Gain 0–100 · Tone 0–100 · PRSNC 0–100 · VOL 0–100 |
| 0x03000080 | MetalWRLD | 10 | ? | Gain 0–100 · Bass 0–100 · Treble 0–100 · VOL 0–100 |
| 0x03000090 | TB MK1.5 | 16 | 1 | ATTCK 0–100 · Tone 0–100 · Color (list) · VOL 0–100 |
| 0x030000a0 | OctFuzz | 25 | 1 | Boost 0–100 · Color (list) · Tone 0–100 · VOL 0–100 |
| 0x030000b0 | SpotBoost | 11 | 1 | Boost 0–100 · Bass −10–10 · Treble −10–10 · ON/OFF (LATCH, UnLATCH) |
| 0x030000c0 | Aco.Sim | 10 | 1 | Top 0–100 · Body 0–100 · Tone 0–100 · VOL 0–100 |
| 0x030000d0 | NYC Muff | 11 | 1 | SUSTN 0–100 · Tone 0–100 · BAL 0–100 · VOL 0–100 |
| 0x030000e0 | HG THRTTL | 22 | 1 | Gain 0–100 · Tone 0–100 · MdCut 0–100 · VOL 0–100 |
| 0x030000f0 | BG GRID | 13 | 1 | Gain 0–100 · Tone 0–100 · BAL 0–100 · VOL 0–100 |
| 0x03000101 | TS+Boost | 11 | ? | Gain 0–100 · Tone 0–100 · VOL 0–100 · Comp 0–2 · Boost 0–100 · Bass 0–100 · Treble 0–100 · Order (BOOST-OD, OD-BOOST) |
| 0x03000111 | RedCR+BST | 10 | ? | Gain 0–100 · Tone 0–100 · PRSNC 0–100 · VOL 0–100 · Comp 0–2 · LO/HI (LO, HI) · Boost 0–100 · Order (BOOST-CR, CR-BOOST) |
| 0x03000120 | DIST 1 | 12 | 1 | Gain 0–100 · Tone 0–100 · VOL 0–100 · Comp (ORG, MOD) |
| 0x03000130 | Squeak | 10 | 1 | Gain 0–100 · FLTR 0–100 · VOL 0–100 · DryMx 0–100 |
| 0x03000140 | UpOctBSTR | 14 | 1 | UpOct 0–100 · DryMx 0–100 · Bottom 0–100 · PRSNC 0–100 |
| 0x03000150 | OutputBST | 4 | 1 | Range 1–10 · Boost 0–100 · Tone 0–100 · VOL 0–100 |
| 0x03000160 | DIST Plus | 10 | 1 | Gain 0–100 · VOL 0–100 · DryMx 0–100 · Comp (ORG, MOD1, MOD2) |
| 0x03000170 | Zen O.DRV | 15 | 1 | Gain 0–100 · Tone 0–100 · Voice 0–100 · VOL 0–100 |
| 0x03000190 | VioletDST | 11 | 1 | Gain 0–100 · Tone 0–100 · Voice 0–2 · VOL 0–100 |
| 0x030001a8 | OverDrive | 10 | 1 | Gain 0–100 · Bass 0–100 · Treble 0–100 · VOL 0–100 |
| 0x03000230 | WaveSHPR | 22 | 1 | Gain 0–100 · Shape 0–100 · Comp 0–100 · VOL 0–100 |
| 0x03000240 | Razor DRV | 17 | 1 | Gain 0–100 · Edge 0–100 · Lo 0–100 · VOL 0–100 |

### Amp (two blocks each)

| Id | Effect | Load % | Blocks | Parameters |
|---|---|---|---|---|
| 0x04000012 | MS 800 | 23 | 2 | GAIN 0–100 · BASS 0–100 · MIDDLE 0–100 · TREBLE 0–100 · PRESENCE 0–100 · VOLUME 0–100 · INPUT (LO, HI) |
| 0x0400001c | MS 1959 | 23 | 2 | GAIN OFF–100 · BASS 0–100 · MIDDLE 0–100 · TREBLE 0–100 · PRESENCE 0–100 · VOLUME 0–100 · INPUT2 OFF–100 · hidden (2) |
| 0x0400001d | MS 45os | 23 | 2 | GAIN OFF–100 · BASS 0–100 · MIDDLE 0–100 · TREBLE 0–100 · PRESENCE 0–100 · VOLUME 0–100 · INPUT2 OFF–100 |
| 0x04000022 | FD TWNR | 22 | 2 | GAIN 0–100 · BASS 0–100 · MIDDLE 0–100 · TREBLE 0–100 · PRESENCE 0–100 · VOLUME 0–100 · BRIGHT (OFF, ON) · SPEED 0–100 |
| 0x04000029 | FD B-MAN | 22 | 2 | GAIN 0–100 · BASS 0–100 · MIDDLE 0–100 · TREBLE 0–100 · PRESENCE 0–100 · VOLUME 0–100 · INPUT (NORMAL, BRIGHT) |
| 0x0400002e | FD DLXR | 22 | 2 | GAIN 0–100 · BASS 0–100 · MIDDLE 0–100 · TREBLE 0–100 · PRESENCE 0–100 · VOLUME 0–100 · INPUT (NORMAL, VIBRATO) · SPEED 0–100 |
| 0x0400002f | FD MASTER | 21 | 2 | GAIN 0–100 · BASS 0–100 · MIDDLE 0–100 · TREBLE 0–100 · PRESENCE 0–100 · VOLUME 0–100 · FAT (OFF, ON) |
| 0x04000032 | UK 30A | 28 | 2 | GAIN 0–100 · BASS 0–100 · MIDDLE 0–100 · TREBLE 0–100 · PRESENCE 0–100 · VOLUME 0–100 · CUT 0–100 · SPEED 0–100 |
| 0x04000044 | BG MK1 | 25 | 2 | GAIN1 0–100 · BASS 0–100 · MIDDLE 0–100 · TREBLE 0–100 · PRESENCE 0–100 · VOLUME 0–100 · GAIN2 0–100 · hidden (2) |
| 0x04000045 | BG MK3 | 25 | 2 | GAIN1 0–100 · BASS 0–100 · MIDDLE 0–100 · TREBLE 0–100 · PRESENCE 0–100 · VOLUME 0–100 · GAIN2 0–100 |
| 0x04000052 | XtasyBlue | 24 | 2 | GAIN 0–100 · BASS 0–100 · MIDDLE 0–100 · TREBLE 0–100 · PRESENCE 0–100 · VOLUME 0–100 · STRCT (LO, HI) |
| 0x04000062 | HW 100 | 19 | 2 | GAIN 0–100 · BASS 0–100 · MIDDLE 0–100 · TREBLE 0–100 · PRESENCE 0–100 · VOLUME 0–100 · INPUT (NORMAL, BRILL) |
| 0x04000072 | Recti ORG | 21 | 2 | GAIN 0–100 · BASS 0–100 · MIDDLE 0–100 · TREBLE 0–100 · PRESENCE 0–100 · VOLUME 0–100 · MODE (VNTG, MDRN) |
| 0x04000082 | ORG120 | 24 | 2 | GAIN 0–100 · BASS 0–100 · MIDDLE 0–100 · TREBLE 0–100 · PRESENCE 0–100 · VOLUME 0–100 · INPUT (LO, HI) · COLOR 1–6 |
| 0x04000092 | DZ DRV | 20 | 2 | GAIN 0–100 · BASS 0–100 · MIDDLE 0–100 · TREBLE 0–100 · PRESENCE 0–100 · VOLUME 0–100 · DEEP 0–100 · MID CUT 0–100 |
| 0x040000a2 | MATCH30 | 26 | 2 | GAIN OFF–100 · BASS 0–100 · MIDDLE 0–100 · TREBLE 0–100 · PRESENCE 0–100 · VOLUME 0–OFF · GAIN2 OFF–100 · CUT 0–100 |
| 0x04080010 | KRAMPUS | 37 | 2 | GAIN · BASS · MIDDLE · TREBLE · PRESENCE · VOLUME (all 0–100) |
| 0x04080020 | REDLOOM | 33 | 2 | GAIN · BASS · MIDDLE · TREBLE · PRESENCE · VOLUME (all 0–100) |
| 0x04080030 | VELVET | 36 | 2 | GAIN · BASS · MIDDLE · TREBLE · PRESENCE · VOLUME (all 0–100) |
| 0x04080040 | MUDDY | 38 | 2 | GAIN · BASS · MIDDLE · TREBLE · PRESENCE · VOLUME (all 0–100) |
| 0x04080050 | 7 HEAVEN | 35 | 2 | GAIN · BASS · MIDDLE · TREBLE · PRESENCE · VOLUME (all 0–100) |
| 0x04080060 | POLLEX | 36 | 2 | GAIN · BASS · MIDDLE · TREBLE · PRESENCE · VOLUME (all 0–100) |

`OFF–100` is stored as shown (OFF = 0). For MS 1959 a stored GAIN of 35 shows 35 on the device.

### Cabinet

All cabinets: Load 7% (8% for KP4x12, RED4x10, VT4x12, 7H4x12), 1 block, parameters MIC (OFF, ON) · D57:D421 0–100 ·
Hi 0–100 · Lo 0–100.

| Id | Cabinet | Id | Cabinet |
|---|---|---|---|
| 0x05000010 | MS4x12 | 0x05000060 | HW4x12 |
| 0x05000018 | MS4x12GB | 0x05000070 | RCT4x12 |
| 0x0500001a | MS4x12AL | 0x05000080 | ORG4x12 |
| 0x05000020 | FD2x12 | 0x05000090 | DZ4x12F |
| 0x05000027 | FD-B4x10 | 0x050000a0 | MA2x12 |
| 0x0500002a | FD-DX1x12 | 0x05080010 | KP4x12 |
| 0x0500002b | FD MA2x12 | 0x05080020 | RED4x10 |
| 0x05000030 | UK2x12 | 0x05080030 | VT4x12 |
| 0x05000040 | MK1 1x12 | 0x05080040 | MD1x12 |
| 0x05000042 | MK3 1x12 | 0x05080050 | 7H4x12 |
| 0x05000050 | BGN4x12 | 0x05080060 | PL4x12 |

### Modulation

| Id | Effect | Load % | Blocks | Parameters |
|---|---|---|---|---|
| 0x06000010 | Tremolo | 7 | 1 | Wave (TRI, TUBE, SQR) · Depth 0–100 · Rate 0–100 · VOL 0–100 |
| 0x06000020 | Chorus | 5 | 1 | Depth 0–100 · Rate 1–50 · Tone 0–10 · Mix 0–100 |
| 0x06000030 | StereoCho | 6 | 1 | Depth 0–100 · Rate 1–50 · Tone 0–10 · Mix 0–100 |
| 0x06000040 | Phaser | 6 | 1 | Color (4 STG, 8 STG, INV 4, INV 8) · Depth 0–100 · Rate 1–50 · RESO 0–100 |
| 0x06000050 | VinFLNGR | 5 | 1 | PreD 0–50 · Depth 0–100 · Rate 0–50 · RESO −10–10 |
| 0x06000060 | TheVibe | 8 | 1 | Speed 0–50 · Depth 0–100 · Mode (VIBRT, CHORS) · VOL 0–100 |
| 0x06000070 | Vibrato | 6 | 1 | Depth 0–100 · Rate 0–50 · Tone 0–10 · BAL 0–100 |
| 0x06000078 | SwellVibe | 8 | 1 | Depth 0–100 · Speed 0–100 · Rise 0–100 · Vol 0–100 |
| 0x06000080 | Octave | 5 | 1 | OCT1 0–100 · OCT2 0–100 · Tone 0–10 · Dry 0–100 |
| 0x06000090 | RingMod | 4 | 1 | FREQ 1–50 · Tone 0–10 · BAL 0–100 · VOL 0–100 |
| 0x060000a0 | Detune | 5 | 1 | Cent −25–25 · PreD 0–50 · Tone 0–10 · Mix 0–100 |
| 0x060000b0 | PitchSHFT | 6 | 1 | Shift (−12…12, 24) · Fine −25–25 · Tone 0–10 · BAL 0–100 |
| 0x060000b8 | PolyShift | 21 | 1 | Shift (−24…24, not contiguous: see above) · Tone 0–100 · Wet 0–100 · Dry 0–100 |
| 0x060000c0 | MonoPitch | 5 | 1 | Shift (−12…12, 24) · Fine −25–25 · Tone 0–10 · BAL 0–100 |
| 0x060000d0 | HPS | 5 | 1 | Scale (−6, −5, −4, −3, −m, m, 3, 4, 5, 6) · Key (C … B) · Tone 0–10 · Mix 0–100 |
| 0x060000e1 | Kick FLNG | 6 | ? | PreD 0–100 · Depth 0–100 · Rate 0–100 · ON/OFF (LATCH, UnLATCH) · RESO 0–100 · Mix 0–100 · RST-F 0–100 |
| 0x060000f0 | Slicer | 7 | 1 | PTTRN 1–20 · Speed 1–50 · THRSH 0–50 · VOL 0–100 |
| 0x06000100 | CloneCho | 8 | 1 | Depth (list) · Rate 0–100 · Tone 0–100 · Mix 0–100 |
| 0x06000110 | SuperCho | 7 | 1 | Depth 0–100 · Rate 0–100 · Tone 0–100 · Mix 0–100 |
| 0x06000120 | StonePha | 6 | 1 | Color (list) · Depth 0–100 · Rate 0–100 · RESO 0–100 |
| 0x06000130 | CoronaTri | 16 | 1 | Depth 0–100 · Speed 0–100 · Tone 0–100 · Mix 0–100 |
| 0x06000140 | BendCho | 7 | 1 | Mode (UP, DOWN) · Depth 0–100 · Time 0–50 · BAL 0–100 |
| 0x06000150 | AnalogCho | 8 | 1 | Depth 0–100 · Rate 0–100 · Tone 0–100 · Mix 0–100 |
| 0x06000160 | WarpPhase | 7 | 1 | Mode (GO, BACK) · Speed 1–50 · RESO 0–10 · VOL 0–100 |
| 0x06000171 | Duo Phase | 8 | ? | DPT A 1–100 · RateA 1–50 · ResoA 0–10 · Link (SERI, PARA, STR) · DPT B 1–100 · Rate B (1–50, SyncA, RvrsA) · Reso B 0–10 · VOL 0–100 |
| 0x06000180 | GEMINOS | 8 | 1 | Tight 0–100 · Mode (MN-3, MN-2, MN-1, ST-1, ST-2, ST-3) · Wet 0–100 · Dry 0–100 |

### SFX

| Id | Effect | Load % | Blocks | Parameters |
|---|---|---|---|---|
| 0x07000010 | Bomber | 7 | 1 | Decay 1–100 · Tone 0–10 · Mix 0–100 · ON/OFF (LATCH, TRGGR) |
| 0x07000020 | AutoPan | 3 | 1 | Rate 0–50 · Width 0–50 · Clip 0–10 · VOL 0–100 |
| 0x07000040 | LoopRoll | 4 | 1 | Time 10–4000 · Duty 25–100 · BAL 0–100 · ON/OFF (LATCH, UnLATCH) |
| 0x07000050 | HotSpice | 15 | 1 | Bend 0–100 · Buzz 0–100 · +1oct 0–100 · VOL 0–100 |
| 0x07000060 | BitCrush | 5 | 1 | Bit 4–16 · SMPL 0–50 · Tone 0–10 · BAL 0–100 |

### Delay

| Id | Effect | Load % | Blocks | Parameters |
|---|---|---|---|---|
| 0x08000010 | Delay | 4 | 1 | Time 1–4000 · F.B 0–100 · Mix 0–100 · Tail (OFF, ON) |
| 0x08000020 | AnalogDly | 4 | 1 | Time 1–4000 · F.B 0–100 · Mix 0–100 · Tail (OFF, ON) |
| 0x08000030 | TapeEcho | 6 | 1 | Time 1–2000 · F.B 0–100 · Mix 0–100 · Tail (OFF, ON) |
| 0x08000040 | ReverseDL | 5 | 1 | Time 10–2000 · F.B 0–100 · BAL 0–100 · Tail (OFF, ON) |
| 0x08000050 | ModDelay | 6 | 1 | Time 1–2000 · F.B 0–100 · Mix 0–100 · Tail (OFF, ON) |
| 0x08000061 | Hold DLY | 5 | ? | Time 1–4000 · F.B 0–100 · HiDMP 0–10 · Tone 0–100 · Mix 0–100 · P-P (MONO, P-P) · Tail (OFF, ON) |
| 0x08000080 | P-P Delay | 5 | 1 | Time 1–4000 · F.B 0–100 · Mix 0–100 · Tail (OFF, ON) |
| 0x08000090 | FilterDly | 7 | 1 | Time 1–2000 · F.B 0–100 · Mix 0–100 · Tail (OFF, ON) |
| 0x080000a1 | Dual DLY | 13 | ? | TimeA 0–1490 · F.B A 0–110 · TimeB 0–1490 · F.B B 0–110 · DlyMx 0–100 · BAL 0–100 · Depth (list) · Speed 0–50 |
| 0x080000b0 | Pitch DLY | 7 | 1 | Pitch −12–12 · Time 1–2000 · F.B 0–100 · Mix 0–100 |
| 0x080000c0 | SlapBackD | 11 | 1 | Time 1–300 · F.B 0–100 · Mix 0–100 · SubDv (list) |
| 0x080000d1 | A-Pan DLY | 8 | ? | Time 1–1500 · F.B 0–100 · Mix 0–100 · Link (PAN-DLY, DLY-PAN) · Cycle (list) · Width 0–50 · Clip 0–10 |
| 0x080000e1 | PhaseDly | 10 | ? | Time 1–2000 · F.B 0–100 · Mix 0–100 · Tail (OFF, ON) · Color (4 STG, 8 STG, INV 4, INV 8) · Depth 0–100 · Rate 1–50 · Reso 0–100 |
| 0x080000f1 | TapeEcho3 | 14 | ? | Gain 0–100 · Hi 0–100 · Lo 0–100 · VOL 0–100 · Time 10–1000 · F.B 0–100 · Mix 0–100 · RecLv 0–100 |
| 0x08000100 | ICE Delay | 21 | 1 | INTVL (list, −OCT…2 OCT) · Time 60–1300 · F.B 0–100 · Mix 0–100 |
| 0x08000110 | SlwAtkDly | 13 | 1 | Swell 1–50 · Time 1–1900 · F.B 0–100 · Mix 0–100 |
| 0x08000120 | SoftEcho | 20 | 1 | MOD (OFF, ON) · Time 19–581 · F.B 0–100 · Mix 0–100 |
| 0x08000130 | DynaDelay | 7 | 1 | Sense −10–10 · Time 1–2000 then note values · F.B 0–100 · Mix 0–100 |

### Reverb

| Id | Effect | Load % | Blocks | Parameters |
|---|---|---|---|---|
| 0x09000010 | Air | 5 | 1 | Size 1–100 · REF 0–10 · Mix 0–100 · Tail (OFF, ON) |
| 0x09000020 | Room | 8 | 1 | PreD 1–100 · Decay 1–30 · Mix 0–100 · Tail (OFF, ON) |
| 0x09000028 | BrghtRoom | 8 | 1 | PreD 1–100 · Decay 1–30 · Tone 0–10 · Mix 0–100 |
| 0x09000030 | Hall | 9 | 1 | PreD 1–100 · Decay 1–30 · Mix 0–100 · Tail (OFF, ON) |
| 0x09000032 | BrghtHall | 9 | 1 | PreD 1–100 · Decay 1–30 · Tone 0–10 · Mix 0–100 |
| 0x09000040 | HD Hall | 21 | 1 | PreD 1–200 · Decay 0–100 · Mix 0–100 · Tail (OFF, ON) |
| 0x09000050 | Spring | 7 | 1 | PreD 1–100 · Decay 1–30 · Mix 0–100 · Tail (OFF, ON) |
| 0x09000060 | FD Spring | 20 | 1 | Color (list) · Lo 0–100 · Hi 0–100 · Mix 0–100 |
| 0x09000070 | Plate | 17 | 1 | PreD 1–200 · Decay 0–100 · Mix 0–100 · Tail (OFF, ON) |
| 0x09000080 | EarlyRef | 8 | 1 | Decay 1–30 · Shape −10–10 · Tone 0–10 · Mix 0–100 |
| 0x09000091 | SpaceHole | 23 | ? | PreD 0–1000 · Decay −100–100 · F.B 0–100 · Mix 0–100 · Depth 0–100 · Speed 0–100 · Size 0–100 |
| 0x090000a0 | Church | 24 | 1 | PreD 0–200 · Decay 0–100 · Mix 0–100 · Tail (OFF, ON) |
| 0x090000b0 | Ambience | 23 | 1 | PreD 0–200 · Decay 0–100 · Mix 0–100 · Tail (OFF, ON) |
| 0x090000c0 | ParticleR | 30 | 1 | Mode (STBL, CRTCL, HZD) · Decay 0–100 · Mix 0–100 · Tail (OFF, ON) |
| 0x090000d0 | Chamber | 18 | 1 | PreD 0–200 · Decay 0–100 · Mix 0–100 · Tail (OFF, ON) |
| 0x090000e0 | GateRev | 16 | 1 | Color 1–5 · Decay 0–100 · Tone 0–100 · BAL 0–100 |
| 0x090000f1 | HoldVerb | 17 | ? | PreD 1–200 · Decay 0–100 · Mix 0–100 · Tail (OFF, ON) · Color 0–100 · LoDMP 0–100 · HiDMP 0–100 |

### Pedal

| Id | Effect | Load % | Blocks | Parameters |
|---|---|---|---|---|
| 0x0b000010 | PDL Vol | 3 | 1 | VOL 0–100 · Min 0–100 · Max 0–100 · Curve (A, B) |
| 0x0b000020 | BlackWah | 6 | 1 | FREQ 0–100 · Range 0–100 · Dry 0–100 · VOL 0–100 |
| 0x0b000030 | ChromeWah | 6 | 1 | FREQ 0–100 · Range 0–100 · Dry 0–100 · VOL 0–100 |
| 0x0b000040 | WAH100 | 5 | 1 | FREQ 0–50 · Depth 0–100 · Dry 0–100 · VOL 0–100 |
| 0x0b000050 | PDL Pitch | 6 | 1 | Bend 0–100 · Color (list) · Tone 0–10 · Mode (UP, DOWN) |
| 0x0b000060 | PDL MnPit | 6 | 1 | Bend 0–100 · Color (list) · Tone 0–10 · Mode (UP, DOWN) |
| 0x0b000070 | PDL Vibe | 8 | 1 | Speed 0–50 · Depth 0–100 · Mode (VIBRAT, CHORS) · VOL 0–100 |
| 0x0b000080 | PDL Drive | 14 | 1 | Gain 0–100 · Tone 0–100 · PRSNC 0–100 · VOL 0–100 |
| 0x0b000090 | PDL PHSR | 5 | 1 | Rate 1–50 · Depth 0–100 · RESO 0–100 · Color (4 STG, 8 STG, INV 4, INV 8) |
| 0x0b0000a0 | PDL Delay | 5 | 1 | InLvl 0–100 · Time 1–4000 · F.B 0–100 · Mix 0–100 |
| 0x0b0000b0 | PDL Rev | 9 | 1 | InLvl 0–100 · PreD 1–100 · Decay 1–30 · Mix 0–100 |
| 0x0b0000c0 | OSC Echo | 14 | 1 | OSC 0–100 · T-Min 19–500 · T-Max 19–500 · Mix 0–100 |
| 0x0b0000d0 | VoiceWah | 10 | 1 | Vowel 0–100 · PTTRN (A, B, C) · Voice 0–100 · Mode (STEP, SOFT) |
| 0x0b0000e0 | PDL Roto | 10 | 1 | Mode (SLOW, FAST) · Drive 0–100 · BAL 0–100 · VOL 0–100 |
| 0x0b0000f0 | P-BitCRSH | 4 | 1 | SMPL 0–50 · Bit 4–32 · Tone 0–10 · BAL 0–100 |
| 0x0b000100 | PDL FLNGR | 5 | 1 | FREQ 0–100 · RESO −10–10 · HiDMP 0–10 · Mix 0–100 |
| 0x0b800020 | PDL Reso | 5 | 1 | FREQ 1–50 · RESO 0–10 · Dry 0–100 · VOL 0–100 |
| 0x0bfffff0 | Output VP | 3 | 1 | — |

### Send / return

| Id | Effect | Load % | Blocks | Parameters |
|---|---|---|---|---|
| 0x1a000020 | FxLoop | 2 | 1 | Send 0–100 · Return 0–100 · Dry 0–100 · Vol 0–100 |
| 0x1a000023 | Send | 2 | 1 | Send 0–100 · Dry 0–100 · Mode (SEND, SUBOUT) · ON/OFF (LATCH, UnLATCH) |
| 0x1a000026 | Return | 2 | 1 | Return 0–100 · Phase (NORM, INV) · Dry 0–100 · Vol 0–100 |
