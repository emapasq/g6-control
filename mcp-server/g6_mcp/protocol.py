"""Zoom G6 USB MIDI protocol: pure functions, no I/O.

Mirrors webapp/public/js/g6-protocol.js. Every message built here is listed as verified in
protocol/COMMANDS.md (firmware 2.00). Rule: the server may only send messages accepted by
assert_safe_to_send() / assert_safe_channel_message().

Conventions: a *body* is a SysEx message without F0, the Zoom/G6 header and F7.
A *frame* is the complete SysEx, F0 and F7 included; reply offsets are zero-based on frames.
"""

from __future__ import annotations

import zlib

PORT_NAME = 'ZOOM G6'
HEADER = [0x52, 0x00, 0x6E]  # Zoom manufacturer id, device 00, G6 model id

SLOT_MIN, SLOT_MAX = 1, 240
BLOCK_MIN, BLOCK_MAX = 0, 8          # effect blocks of the chain
EFFECT_PARAM_MIN, EFFECT_PARAM_MAX = 2, 31  # 0 = on/off (never sent as a parameter), 1 = effect type
PATCH_SIZE = 808                     # PTCF block size on the device
PACKED_PATCH_SIZE = 924              # 808 bytes packed 7-bit
SYSTEM_BLOCK = 0x64                  # block 100: global/system parameters

RHYTHM_PARAMS = {
    'bpm': {'param': 0x02, 'min': 40, 'max': 250},
    'pattern': {'param': 0x5A, 'min': 0, 'max': 67},
    'volume': {'param': 0x5B, 'min': 0, 'max': 100},
    'count': {'param': 0x5C, 'min': 0, 'max': 1},
}

# Rhythm pattern names as listed in the owner's manual. Index = protocol value = manual No. - 1.
RHYTHM_PATTERNS = (
    'GUIDE', '8Beats1', '8Beats2', '8Beats3', '16Beats1', '16Beats2', '16Beats3', 'Rock1', 'Rock2', 'Rock3',
    'ROCKABLY', "R'n'R", 'HardRock', 'HeavyMtl', 'MtlCore', 'Punk', 'FastPunk', 'Emo', 'TomTomBt', 'Funk1',
    'Funk2', 'FunkRock', 'JazzFunk', 'R&B1', 'R&B2', "70's Soul", "90's Soul", 'Motown', 'HipHop', 'Disco',
    'Pop', 'PopRock', 'IndiePop', 'EuroPop', 'NewWave', 'OneDrop', 'Steppers', 'Rockers', 'Ska', '2nd Line',
    'Country', 'Shuffle1', 'Shuffle2', 'Blues1', 'Blues2', 'Jazz1', 'Jazz2', 'Fusion', 'Swing1', 'Swing2',
    'Bossa1', 'Bossa2', 'Samba1', 'Samba2', 'Breaks1', 'Breaks2', 'Breaks3', '12/8 Grv', 'Waltz', 'JzWaltz1',
    'JzWaltz2', 'CtWaltz1', 'CtWaltz2', '5/4 Grv', 'Metro3', 'Metro4', 'Metro5', 'Metro',
)

WAV_LIST_MAX_RECORDS = 50
WAV_MAX_FILES = 256
EMPTY_PATCH_NAME = 'Empty'
RHYTHM_STATE_LENGTH = 61


class ProtocolError(ValueError):
    """A reply that does not match its verified format, or an invalid request."""


class BlockedMessage(ValueError):
    """A message that is not in the verified list: it is never sent."""


# ---------- encoding helpers ----------

def val5(value: int) -> list[int]:
    """Integer as 5 bytes of 7 bits, least significant first."""
    if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value < 1 << 35:
        raise ProtocolError(f'value out of range: {value!r}')
    return [(value >> (7 * k)) & 0x7F for k in range(5)]


def decode7(data) -> int:
    """Decode groups of 7 bits, least significant first."""
    value = 0
    for k, x in enumerate(data):
        value |= (x & 0x7F) << (7 * k)
    return value


def pack7(data: bytes) -> list[int]:
    """Zoom 7-bit packing: groups of 7 bytes preceded by a byte holding their high bits (bit 6 = first)."""
    out = []
    for i in range(0, len(data), 7):
        group = data[i:i + 7]
        high = 0
        for j, x in enumerate(group):
            if x & 0x80:
                high |= 1 << (6 - j)
        out += [high] + [x & 0x7F for x in group]
    return out


def unpack7(data) -> bytes:
    """Inverse of pack7."""
    out = bytearray()
    for i in range(0, len(data), 8):
        high = data[i]
        for j, x in enumerate(data[i + 1:i + 8]):
            out.append(x | (0x80 if (high >> (6 - j)) & 1 else 0))
    return bytes(out)


def crc5(data: bytes) -> list[int]:
    """Patch CRC: crc32(data) ^ 0xFFFFFFFF as 5 x 7 bits, least significant first."""
    crc = zlib.crc32(data) ^ 0xFFFFFFFF
    return [(crc >> (7 * k)) & 0x7F for k in range(5)]


def slot_to_bank_pos(slot: int) -> tuple[int, int]:
    return divmod(slot - 1, 4)


def bank_pos_to_slot(bank: int, pos: int) -> int:
    return bank * 4 + pos + 1


def is_valid_slot(slot) -> bool:
    return isinstance(slot, int) and not isinstance(slot, bool) and SLOT_MIN <= slot <= SLOT_MAX


def is_valid_block(block) -> bool:
    return isinstance(block, int) and not isinstance(block, bool) and BLOCK_MIN <= block <= BLOCK_MAX


def is_valid_rhythm_value(name: str, value) -> bool:
    spec = RHYTHM_PARAMS.get(name)
    return (spec is not None and isinstance(value, int) and not isinstance(value, bool)
            and spec['min'] <= value <= spec['max'])


def to_sysex(body) -> list[int]:
    return [0xF0, *HEADER, *body, 0xF7]


def body_of(frame) -> list[int] | None:
    """Body of a complete G6 SysEx frame, or None."""
    frame = list(frame)
    if len(frame) < 5 or frame[0] != 0xF0 or frame[-1] != 0xF7 or frame[1:4] != HEADER:
        return None
    return frame[4:-1]


def hexs(data) -> str:
    return ' '.join(f'{x:02X}' for x in data)


# ---------- PTCF (.zptc) ----------

def ptcf_name(ptcf: bytes) -> str:
    """Patch name (bytes 26-35, ASCII)."""
    return ptcf[26:36].replace(b'\0', b'').decode('ascii', 'replace').rstrip()


AMP_SECOND_HALF_ID = 0x00000001  # second chain entry of a two-block amp


def ptcf_effect_ids(ptcf: bytes) -> list[int]:
    """Effect id of each chain entry, in chain order: the count is at 0x0C, the ids (uint32 LE) start at 0x24.
    An amp takes two entries (its id, then 0x00000001); 0 = empty. Entry k is display block k."""
    n = int.from_bytes(ptcf[12:16], 'little')
    if not 0 <= n <= BLOCK_MAX + 1 or len(ptcf) < 0x24 + 4 * n:
        raise ProtocolError('invalid effect list in the patch')
    return [int.from_bytes(ptcf[0x24 + 4 * k:0x28 + 4 * k], 'little') for k in range(n)]


def check_ptcf(ptcf: bytes) -> None:
    if ptcf[:4] != b'PTCF':
        raise ProtocolError('not a PTCF patch (.zptc)')
    if not int.from_bytes(ptcf[4:8], 'little') == len(ptcf) <= PATCH_SIZE:
        raise ProtocolError('invalid PTCF length')


def _trim_ptcf(data: bytes) -> bytes:
    length = int.from_bytes(data[4:8], 'little')
    if data[:4] != b'PTCF' or not 8 <= length <= len(data):
        raise ProtocolError('patch dump without a valid PTCF block')
    return data[:length]


# ---------- messages to the G6 ----------

MSG = {
    'editor_mode': [0x50],
    'enter_pc_mode': [0x52],
    'exit_pc_mode': [0x53],
    'host_ack': [0x00, 0x00],
    'request_active_slot': [0x33],
    'request_active_patch': [0x64, 0x13],
    'request_rhythm_state': [0x64, 0x0A],
    'rhythm_start': [0x64, 0x20, 0x00, 0x5B, 0x00, 0x01, 0, 0, 0, 0],
    'rhythm_stop': [0x64, 0x20, 0x00, 0x5B, 0x01, 0x01, 0, 0, 0, 0],
    'request_sd_space': [0x64, 0x3C],
    'request_wav_count': [0x64, 0x35],
    'effects_list_start': [0x64, 0x04],
    'effects_list_next': [0x64, 0x05],
    'tuner_on': [0x64, 0x0B],
    'tuner_off': [0x64, 0x0C],
    'write_begin': [0x56],
    'write_end': [0x57],
    'toggle_press': [0x64, 0x20, 0x00, 0x5A, 0x00, 0x01, 0, 0, 0, 0],
    'toggle_release': [0x64, 0x20, 0x00, 0x5A, 0x00, 0x00, 0, 0, 0, 0],
    # Looper block 5C. Parameter 0 is the main foot-switch gesture: it plays a loaded, stopped loop, but can record
    # (empty Looper) or overdub (while playing). Send only through the gated flow (load, confirmation, one shot).
    'looper_play': [0x64, 0x20, 0x00, 0x5C, 0x00, 0x01, 0, 0, 0, 0],
    'looper_stop': [0x64, 0x20, 0x00, 0x5C, 0x01, 0x01, 0, 0, 0, 0],
}


def read_stored_patch_msg(slot: int) -> list[int]:
    if not is_valid_slot(slot):
        raise ProtocolError(f'slot out of range: {slot}')
    bank, pos = slot_to_bank_pos(slot)
    return [0x46, 0x00, 0x00, bank, 0x00, pos, 0x00]


def write_stored_patch_msg(slot: int, ptcf: bytes) -> list[int]:
    """45 00 00 BB 00 PP 00 28 06 <packed 808 bytes> <CRC>: overwrites the slot (send between 56 and 57)."""
    if not is_valid_slot(slot):
        raise ProtocolError(f'slot out of range: {slot}')
    check_ptcf(ptcf)
    bank, pos = slot_to_bank_pos(slot)
    buf = ptcf + bytes(PATCH_SIZE - len(ptcf))
    return [0x45, 0, 0, bank, 0, pos, 0, PATCH_SIZE & 0x7F, PATCH_SIZE >> 7] + pack7(buf) + crc5(buf)


def select_block_msg(block: int) -> list[int]:
    if not is_valid_block(block):
        raise ProtocolError(f'block out of range: {block}')
    return [0x64, 0x20, 0x00, SYSTEM_BLOCK, 0x01, block, 0, 0, 0, 0]


def effect_param_msg(block: int, param: int, value: int) -> list[int]:
    """Live effect parameter (raw value). Parameter 0 (on/off) is never sent this way: it froze the device."""
    if not is_valid_block(block):
        raise ProtocolError(f'block out of range: {block}')
    if not isinstance(param, int) or not EFFECT_PARAM_MIN <= param <= EFFECT_PARAM_MAX:
        raise ProtocolError(f'parameter out of range: {param}')
    return [0x64, 0x20, 0x00, block, param, *val5(value)]


def display_request_msg(first: int = BLOCK_MIN, last: int = BLOCK_MAX) -> list[int]:
    if not (is_valid_block(first) and is_valid_block(last) and first <= last):
        raise ProtocolError('invalid display range')
    return [0x64, 0x02, first, last, 0x00]


def set_rhythm_param_msg(name: str, value: int) -> list[int]:
    if not is_valid_rhythm_value(name, value):
        raise ProtocolError(f'invalid {name}: {value!r}')
    return [0x64, 0x20, 0x00, SYSTEM_BLOCK, RHYTHM_PARAMS[name]['param'], *val5(value)]


def wav_list_msg(start: int, end: int, known_count: int) -> list[int]:
    """WAV list request for the inclusive range start..end; known_count comes from a WAV count read."""
    ints = all(isinstance(x, int) and not isinstance(x, bool) for x in (start, end, known_count))
    if not ints or not 1 <= known_count <= WAV_MAX_FILES or not 0 <= start <= end < known_count:
        raise ProtocolError('invalid WAV list range')
    if end - start + 1 > WAV_LIST_MAX_RECORDS:
        raise ProtocolError(f'at most {WAV_LIST_MAX_RECORDS} records per request')
    return [0x64, 0x37, start & 0x7F, start >> 7, end & 0x7F, end >> 7]


# ---------- Looper settings (system block 64) ----------
# Looper volume: setter 64 20 00 64 5F <val5> (0-100) and readback 64 09 byte 46, verified on the device.
# STOP MODE: setter 64 20 00 64 5E <val5> and readback 64 09 byte 45, verified on the device for 0 INSTANT,
# 1 FINISH and 2 FADE OUT (setting only: not the audio behaviour of a stop).
# POSITION: setter 64 20 00 64 61 <val5> and readback byte 48, verified on the device for 0 PRE-EFFECTS and
# 1 POST-EFFECTS. Changing it stops and resets the Looper, even when the value is the same: never resend it.
# LOOP MODE (byte 49) is read only: with an SD card inserted the Looper is fixed to STEREO whatever it says.

STOP_MODE_PARAM = 0x5E
LOOPER_VOLUME_PARAM = 0x5F   # Looper volume 0-100: setter and readback (byte 46) verified on the device
LOOPER_VOLUME_MAX = 100
STOP_MODES = {0: 'INSTANT', 1: 'FINISH', 2: 'FADE OUT'}
POSITION_PARAM = 0x61
LOOPER_POSITIONS = {0: 'PRE-EFFECTS', 1: 'POST-EFFECTS'}     # both verified: setter, readback and display
LOOP_MODES = {0: 'MONO'}                                     # 0 read on the device; other values not verified
LOOP_MODES_UNVERIFIED = {1: 'STEREO (not verified)'}


def set_stop_mode_msg(mode: int) -> list[int]:
    if not isinstance(mode, int) or isinstance(mode, bool) or mode not in STOP_MODES:
        raise ProtocolError(f'invalid STOP MODE: {mode!r} (0 INSTANT, 1 FINISH, 2 FADE OUT)')
    return [0x64, 0x20, 0x00, SYSTEM_BLOCK, STOP_MODE_PARAM, *val5(mode)]


def set_position_msg(position: int) -> list[int]:
    if not isinstance(position, int) or isinstance(position, bool) or position not in LOOPER_POSITIONS:
        raise ProtocolError(f'invalid Looper POSITION: {position!r} (0 PRE-EFFECTS, 1 POST-EFFECTS)')
    return [0x64, 0x20, 0x00, SYSTEM_BLOCK, POSITION_PARAM, *val5(position)]


def parse_position_change(b) -> dict | None:
    """64 20 0x 64 61 <val5> -> {'value', 'confirmed'}, or None."""
    if len(b) < 10 or b[:2] != [0x64, 0x20] or b[2] > 1 or b[3] != SYSTEM_BLOCK or b[4] != POSITION_PARAM:
        return None
    return {'value': decode7(b[5:10]), 'confirmed': b[2] == 1}


def set_looper_volume_msg(volume: int) -> list[int]:
    if not isinstance(volume, int) or isinstance(volume, bool) or not 0 <= volume <= LOOPER_VOLUME_MAX:
        raise ProtocolError(f'invalid Looper volume: {volume!r} (0-{LOOPER_VOLUME_MAX})')
    return [0x64, 0x20, 0x00, SYSTEM_BLOCK, LOOPER_VOLUME_PARAM, *val5(volume)]


def parse_looper_volume_change(b) -> dict | None:
    """64 20 0x 64 5F <val5> -> {'value', 'confirmed'}, or None."""
    if len(b) < 10 or b[:2] != [0x64, 0x20] or b[2] > 1 or b[3] != SYSTEM_BLOCK or b[4] != LOOPER_VOLUME_PARAM:
        return None
    return {'value': decode7(b[5:10]), 'confirmed': b[2] == 1}


def parse_stop_mode_change(b) -> dict | None:
    """64 20 0x 64 5E <val5> -> {'value', 'confirmed'} (third byte 01 = confirmation of a setter), or None."""
    if len(b) < 10 or b[:2] != [0x64, 0x20] or b[2] > 1 or b[3] != SYSTEM_BLOCK or b[4] != STOP_MODE_PARAM:
        return None
    return {'value': decode7(b[5:10]), 'confirmed': b[2] == 1}


def parse_looper_settings(frame) -> dict:
    """Looper settings from the 64 09 frame (zero-based offsets on the complete frame): STOP MODE 45,
    volume 46, POSITION 48, LOOP MODE 49. Settings, not the transport state: playing/stopped is not part of the frame."""
    if not is_rhythm_state_frame(frame):
        raise ProtocolError('invalid rhythm state frame')
    stop, position, loop = frame[45], frame[48], frame[49]
    return {
        'stopMode': stop,
        'stopModeName': STOP_MODES.get(stop, f'unknown ({stop})'),
        'position': position,
        'positionName': LOOPER_POSITIONS.get(position, f'unknown ({position})'),
        'loopMode': loop,
        'loopModeName': LOOP_MODES.get(loop) or LOOP_MODES_UNVERIFIED.get(loop) or f'not qualified ({loop})',
        'volume': frame[46],
    }


def wav_load_msg(index: int, known_count: int) -> list[int]:
    """WAV load (64 39 LL HH): the Looper loads the WAV at a 0-based index of the device's own scan of the
    looper folder. Changes the Looper session (work files, possibly the global BPM): not a read."""
    ints = all(isinstance(x, int) and not isinstance(x, bool) for x in (index, known_count))
    if not ints or not 1 <= known_count <= WAV_MAX_FILES or not 0 <= index < known_count:
        raise ProtocolError('invalid WAV index')
    return [0x64, 0x39, index & 0x7F, index >> 7]


def find_wav_index(names: list[str], wanted: str) -> int:
    """Index of a file name in a WAV list (case-insensitive, as on the SD card's file system).
    Raises ProtocolError when the name is missing or matches more than one entry."""
    if not isinstance(wanted, str) or not wanted.strip() or '\0' in wanted or len(wanted) > 63:
        raise ProtocolError('invalid file name')
    key = wanted.strip().casefold()
    hits = [i for i, n in enumerate(names) if n.casefold() == key]
    if not hits:
        raise ProtocolError(f'"{wanted}" is not in the WAV list')
    if len(hits) > 1:
        raise ProtocolError(f'"{wanted}" matches {len(hits)} entries (indices {hits}): ambiguous')
    return hits[0]


def patch_select_messages(slot: int) -> list[list[int]]:
    """Standard MIDI patch change on channel 1: CC0 = 0, CC32 = bank, Program Change = position."""
    if not is_valid_slot(slot):
        raise ProtocolError(f'slot out of range: {slot}')
    bank, pos = slot_to_bank_pos(slot)
    return [[0xB0, 0, 0], [0xB0, 32, bank], [0xC0, pos]]


# ---------- safety allowlist ----------

_FIXED = [tuple(m) for m in MSG.values()]


def _seven_bit(data) -> bool:
    return all(0 <= x <= 0x7F for x in data)


def _is_rhythm_setter(b) -> bool:
    if len(b) != 10 or b[:4] != [0x64, 0x20, 0x00, SYSTEM_BLOCK] or not _seven_bit(b[5:]):
        return False
    name = next((n for n, s in RHYTHM_PARAMS.items() if s['param'] == b[4]), None)
    return name is not None and is_valid_rhythm_value(name, decode7(b[5:]))


def _is_select_block(b) -> bool:
    return len(b) == 10 and b[:5] == [0x64, 0x20, 0x00, SYSTEM_BLOCK, 0x01] and is_valid_block(b[5]) and b[6:] == [0] * 4


def _is_effect_param(b) -> bool:
    return (len(b) == 10 and b[:3] == [0x64, 0x20, 0x00] and is_valid_block(b[3])
            and EFFECT_PARAM_MIN <= b[4] <= EFFECT_PARAM_MAX and _seven_bit(b[5:]))


def _is_display_request(b) -> bool:
    return len(b) == 5 and b[:2] == [0x64, 0x02] and is_valid_block(b[2]) and is_valid_block(b[3]) \
        and b[2] <= b[3] and b[4] == 0


def _is_stored_read(b) -> bool:
    return len(b) == 7 and b[0] == 0x46 and b[1:3] == [0, 0] and b[3] < 60 and b[4] == 0 and b[5] < 4 and b[6] == 0


def _is_stored_write(b) -> bool:
    if len(b) != 9 + PACKED_PATCH_SIZE + 5 or b[0] != 0x45 or b[1:3] != [0, 0] or b[3] >= 60 or b[4] != 0 \
            or b[5] >= 4 or b[6] != 0 or b[7:9] != [PATCH_SIZE & 0x7F, PATCH_SIZE >> 7] or not _seven_bit(b):
        return False
    data = unpack7(b[9:9 + PACKED_PATCH_SIZE])[:PATCH_SIZE]
    return data[:4] == b'PTCF' and crc5(data) == b[-5:]


def _is_wav_list_request(b) -> bool:
    if len(b) != 6 or b[:2] != [0x64, 0x37] or not _seven_bit(b[2:]):
        return False
    start, end = b[2] | b[3] << 7, b[4] | b[5] << 7
    return start <= end < WAV_MAX_FILES and end - start + 1 <= WAV_LIST_MAX_RECORDS


def _is_stop_mode_setter(b) -> bool:
    return (len(b) == 10 and b[:5] == [0x64, 0x20, 0x00, SYSTEM_BLOCK, STOP_MODE_PARAM] and _seven_bit(b[5:])
            and decode7(b[5:]) in STOP_MODES)


def _is_looper_volume_setter(b) -> bool:
    return (len(b) == 10 and b[:5] == [0x64, 0x20, 0x00, SYSTEM_BLOCK, LOOPER_VOLUME_PARAM] and _seven_bit(b[5:])
            and decode7(b[5:]) <= LOOPER_VOLUME_MAX)


def _is_position_setter(b) -> bool:
    return (len(b) == 10 and b[:5] == [0x64, 0x20, 0x00, SYSTEM_BLOCK, POSITION_PARAM] and _seven_bit(b[5:])
            and decode7(b[5:]) in LOOPER_POSITIONS)


def _is_wav_load(b) -> bool:
    return len(b) == 4 and b[:2] == [0x64, 0x39] and _seven_bit(b[2:]) and (b[2] | b[3] << 7) < WAV_MAX_FILES


def is_safe_to_send(body) -> bool:
    b = list(body)
    return (tuple(b) in _FIXED or _is_rhythm_setter(b) or _is_select_block(b) or _is_effect_param(b)
            or _is_display_request(b) or _is_stored_read(b) or _is_stored_write(b) or _is_wav_list_request(b)
            or _is_wav_load(b) or _is_stop_mode_setter(b) or _is_looper_volume_setter(b)
            or _is_position_setter(b))


def assert_safe_to_send(body) -> None:
    if not is_safe_to_send(body):
        raise BlockedMessage(f'Blocked: message not in the verified list ({hexs(list(body)[:16])})')


def assert_safe_channel_message(data) -> None:
    d = list(data)
    ok = (len(d) == 3 and d[0] == 0xB0 and d[1] in (0, 32) and d[2] < 128) or \
         (len(d) == 2 and d[0] == 0xC0 and d[1] < 4)
    if not ok:
        raise BlockedMessage('Blocked: channel message not in the verified list')


# ---------- parsing replies and notifications (bodies unless stated) ----------

def is_ok(b) -> bool:
    return len(b) >= 2 and b[0] == 0x00 and b[1] == 0x00


def is_active_patch_dump(b) -> bool:
    return len(b) >= 2 and b[0] == 0x64 and b[1] == 0x12


def parse_active_patch(b) -> bytes:
    """PTCF from 64 12 01 28 06 <packed PTCF> <CRC 5 bytes>."""
    return _trim_ptcf(unpack7(b[5:-5]))


def is_stored_patch_dump(b, slot: int) -> bool:
    bank, pos = slot_to_bank_pos(slot)
    return len(b) > 9 and b[0] == 0x45 and b[3] == bank and b[5] == pos


def parse_stored_patch(b) -> bytes:
    """PTCF from 45 00 00 BB 00 PP 00 28 06 <packed PTCF> <CRC 5 bytes>."""
    return _trim_ptcf(unpack7(b[9:-5]))


def is_display(b) -> bool:
    return len(b) >= 2 and b[0] == 0x64 and b[1] == 0x01


def parse_display(b) -> dict:
    """64 01 + 14-byte entries (block, parameter, type 0 = value / 1 = label, 00, 10 ASCII chars).

    Returns {block: {param: {'label': str, 'value': str}}}.
    """
    data, blocks = b[2:], {}
    for k in range(0, len(data) - 13, 14):
        block, param, kind = data[k], data[k + 1], data[k + 2]
        text = bytes(data[k + 4:k + 14]).split(b'\0')[0].decode('ascii', 'replace')
        blocks.setdefault(block, {}).setdefault(param, {})['label' if kind == 1 else 'value'] = text
    return blocks


def effect_name(blocks: dict, block: int) -> str:
    return blocks.get(block, {}).get(1, {}).get('value', '')


def is_amp_page2(blocks: dict, block: int) -> bool:
    """An amp uses two blocks: the second shows the same name and has no on/off of its own."""
    name = effect_name(blocks, block)
    return bool(name) and block > 0 and name == effect_name(blocks, block - 1)


def parse_patch_changed(b) -> int | None:
    """Footswitch notification 64 26 00 00 BB 00 PP 00 -> slot."""
    if len(b) < 7 or b[0] != 0x64 or b[1] != 0x26:
        return None
    return bank_pos_to_slot(b[4], b[6])


def is_effect_confirmation(b, block: int, param: int) -> bool:
    """64 20 01 BB PP <val5>: confirmation of a live change."""
    return len(b) >= 10 and b[:5] == [0x64, 0x20, 0x01, block, param]


def parse_rhythm_param(b) -> dict | None:
    """64 20 0x 64 PP <val5> -> {'name', 'value', 'confirmed'}; third byte 01 = setter confirmation."""
    if len(b) < 10 or b[0] != 0x64 or b[1] != 0x20 or b[2] > 1 or b[3] != SYSTEM_BLOCK:
        return None
    name = next((n for n, s in RHYTHM_PARAMS.items() if s['param'] == b[4]), None)
    return {'name': name, 'value': decode7(b[5:10]), 'confirmed': b[2] == 1} if name else None


def is_rhythm_confirmation(b, name: str) -> bool:
    p = parse_rhythm_param(b)
    return bool(p and p['confirmed'] and p['name'] == name)


def _is_reply_frame(frame, command: int, length: int | None = None) -> bool:
    b = body_of(frame)
    return b is not None and len(b) >= 2 and b[0] == 0x64 and b[1] == command and (length is None or len(frame) == length)


def is_rhythm_state_frame(frame) -> bool:
    return _is_reply_frame(frame, 0x09, RHYTHM_STATE_LENGTH)


def parse_rhythm_state(frame) -> dict:
    """64 09, 61 bytes: BPM at 6-7, pattern 41, volume 42, COUNT 43. Playback state is not part of it."""
    if not is_rhythm_state_frame(frame):
        raise ProtocolError('invalid rhythm state frame')
    return {'bpm': decode7(frame[6:8]), 'pattern': frame[41], 'volume': frame[42], 'count': frame[43],
            'playing': None, 'playingVerified': False}


def _decode7_wide(data) -> int:
    return decode7(data)


def is_sd_space_frame(frame) -> bool:
    return _is_reply_frame(frame, 0x3D, 27)


def parse_sd_space(frame) -> dict:
    """64 3D: total at 6-15, free at 16-25 (10 x 7 bits each, bytes)."""
    if not is_sd_space_frame(frame):
        raise ProtocolError('invalid SD space frame')
    total, free = _decode7_wide(frame[6:16]), _decode7_wide(frame[16:26])
    if free > total:
        raise ProtocolError('SD free space exceeds total')
    return {'totalBytes': total, 'freeBytes': free}


def is_wav_count_frame(frame) -> bool:
    return _is_reply_frame(frame, 0x36, 9)


def parse_wav_count(frame) -> int:
    """64 36: number of WAV files in the device cache, at 6-7."""
    if not is_wav_count_frame(frame):
        raise ProtocolError('invalid WAV count frame')
    count = _decode7_wide(frame[6:8])
    if count > WAV_MAX_FILES:
        raise ProtocolError('WAV count out of range')
    return count


def is_wav_list_frame(frame) -> bool:
    return _is_reply_frame(frame, 0x38) and len(frame) == 8 + 64 * frame[6]


def parse_wav_list(frame, start: int, end: int) -> list[str]:
    """64 38: record count at 6, then 64-byte NUL-padded names; the count must match the request."""
    if not is_wav_list_frame(frame):
        raise ProtocolError('invalid WAV list frame')
    count = frame[6]
    if count != end - start + 1:
        raise ProtocolError('WAV list does not match the requested range: the cache may have changed')
    names = []
    for i in range(count):
        record = bytes(frame[7 + 64 * i:7 + 64 * (i + 1)])
        names.append(record.split(b'\0')[0].decode('ascii', 'replace'))
    return names


# ---------- effect list (64 04 / 64 05 -> 64 06 ... 64 07) ----------

def is_effect_record_frame(frame) -> bool:
    return _is_reply_frame(frame, 0x06) and len(frame) >= 38


def is_effects_end(b) -> bool:
    return len(b) >= 2 and b[0] == 0x64 and b[1] == 0x07


def parse_effect_record(frame) -> dict:
    """64 06: module file name at 6-18 (NUL padded), parameter count N at 36, then N-1 records of 11 bytes
    from offset 37 (parameter 0, then 2..N-1; parameter 1, the effect type, is omitted).
    Each record: max (2 x 7 bits), default (2 x 7 bits), then fields not used here.

    Returns {'file': 'TS_DRIVE.ZD2', 'params': {n: {'max': int, 'default': int}}} for n >= 2.
    """
    if not is_effect_record_frame(frame):
        raise ProtocolError('invalid effect record frame')
    name = bytes(frame[6:19]).split(b'\0')[0].decode('ascii', 'replace')
    n = frame[36]
    if n < 2 or len(frame) != 27 + 11 * n:
        raise ProtocolError(f'effect record {name!r}: unexpected length')
    params = {}
    for i in range(1, n - 1):            # record 0 is parameter 0 (on/off)
        r = frame[37 + 11 * i:37 + 11 * (i + 1)]
        params[i + 1] = {'max': decode7(r[0:2]), 'default': decode7(r[2:4])}
    return {'file': name, 'params': params}


def is_ir_file(file_name: str) -> bool:
    stem = file_name.rsplit('.', 1)[0]
    return stem.startswith('IR') and stem[2:].isdigit()


# ---------- WAV load replies ----------

# Generic reply 00 RR. Only 00 was seen after a WAV load on a real G6; other values are not documented. The reply
# carries no opcode or index: it cannot be tied to a request with certainty.
WAV_LOAD_RESULTS = {
    0x00: 'completed',
}


def ack_result(b) -> int | None:
    """Result code of a generic reply 00 RR, or None."""
    return b[1] if len(b) == 2 and b[0] == 0x00 else None


def parse_looper_file_event(b) -> str | None:
    """Looper notification 64 3B + 64 ASCII bytes (NUL padded): the current file name, or None."""
    if len(b) != 66 or b[0] != 0x64 or b[1] != 0x3B:
        return None
    return bytes(b[2:66]).split(b'\0')[0].decode('ascii', 'replace')


def is_tuner_echo(b, on: bool) -> bool:
    return list(b[:2]) == (MSG['tuner_on'] if on else MSG['tuner_off'])
