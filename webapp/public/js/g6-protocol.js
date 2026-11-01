// Zoom G6 USB protocol: pure functions, no I/O.
// Every message here is listed as verified in protocol/COMMANDS.md (firmware 2.00).
// Rule: the app may only send messages accepted by assertSafeToSend().

export const PORT_NAME = 'ZOOM G6';
export const HEADER = [0x52, 0x00, 0x6e]; // Zoom manufacturer id, device 00, G6 model id

export const SLOT_MIN = 1;
export const SLOT_MAX = 240;

const SYSTEM_BLOCK = 0x64; // block 100: global/system parameters

/** Rhythm parameters of the system block, with their valid ranges. */
export const RHYTHM_PARAMS = Object.freeze({
  bpm: { param: 0x02, min: 40, max: 250 },
  pattern: { param: 0x5a, min: 0, max: 67 },
  volume: { param: 0x5b, min: 0, max: 100 },
  count: { param: 0x5c, min: 0, max: 1 },
});

export const BPM_MIN = RHYTHM_PARAMS.bpm.min;
export const BPM_MAX = RHYTHM_PARAMS.bpm.max;

/** Rhythm pattern names, as listed in the G6 owner's manual ("Rhythm patterns"). Index = protocol value = manual No. - 1. */
export const RHYTHM_PATTERNS = Object.freeze([
  'GUIDE', '8Beats1', '8Beats2', '8Beats3', '16Beats1', '16Beats2', '16Beats3', 'Rock1', 'Rock2', 'Rock3',
  'ROCKABLY', "R'n'R", 'HardRock', 'HeavyMtl', 'MtlCore', 'Punk', 'FastPunk', 'Emo', 'TomTomBt', 'Funk1',
  'Funk2', 'FunkRock', 'JazzFunk', 'R&B1', 'R&B2', "70's Soul", "90's Soul", 'Motown', 'HipHop', 'Disco',
  'Pop', 'PopRock', 'IndiePop', 'EuroPop', 'NewWave', 'OneDrop', 'Steppers', 'Rockers', 'Ska', '2nd Line',
  'Country', 'Shuffle1', 'Shuffle2', 'Blues1', 'Blues2', 'Jazz1', 'Jazz2', 'Fusion', 'Swing1', 'Swing2',
  'Bossa1', 'Bossa2', 'Samba1', 'Samba2', 'Breaks1', 'Breaks2', 'Breaks3', '12/8 Grv', 'Waltz', 'JzWaltz1',
  'JzWaltz2', 'CtWaltz1', 'CtWaltz2', '5/4 Grv', 'Metro3', 'Metro4', 'Metro5', 'Metro',
]);

// ---------- encoding helpers ----------

/** Encode a value as 5 bytes of 7 bits, LSB first. */
export function val5(value) {
  const out = [];
  for (let k = 0; k < 5; k++) out.push((value >> (7 * k)) & 0x7f);
  return out;
}

/** Decode up to 5 bytes of 7 bits, LSB first. */
export function decode7(bytes) {
  let value = 0;
  for (let k = 0; k < bytes.length && k < 5; k++) value += (bytes[k] & 0x7f) * 2 ** (7 * k);
  return value;
}

/** Zoom 7-bit unpacking: groups of 8 bytes, first byte holds the high bits (bit 6 = first data byte). */
export function unpack7(bytes) {
  const out = [];
  for (let i = 0; i < bytes.length; i += 8) {
    const highBits = bytes[i];
    for (let j = 1; j < 8 && i + j < bytes.length; j++) {
      const high = (highBits >> (6 - (j - 1))) & 1 ? 0x80 : 0;
      out.push(bytes[i + j] | high);
    }
  }
  return out;
}

/** Patch name stored in a PTCF block (bytes 26-35, ASCII). */
export function ptcfName(ptcf) {
  return String.fromCharCode(...ptcf.slice(26, 36))
    .replace(/\0/g, '')
    .trimEnd();
}

export function slotToBankPos(slot) {
  return { bank: Math.floor((slot - 1) / 4), pos: (slot - 1) % 4 };
}

export function bankPosToSlot(bank, pos) {
  return bank * 4 + pos + 1;
}

export function isValidSlot(slot) {
  return Number.isInteger(slot) && slot >= SLOT_MIN && slot <= SLOT_MAX;
}

export function isValidRhythmValue(name, value) {
  const spec = RHYTHM_PARAMS[name];
  return Boolean(spec) && Number.isInteger(value) && value >= spec.min && value <= spec.max;
}

/** Wrap a message body into a full SysEx frame. */
export function toSysex(body) {
  return [0xf0, ...HEADER, ...body, 0xf7];
}

/** Return the body of a G6 SysEx frame (without F0, header and F7), or null. */
export function bodyOf(bytes) {
  if (bytes.length < 5 || bytes[0] !== 0xf0 || bytes[bytes.length - 1] !== 0xf7) return null;
  for (let k = 0; k < HEADER.length; k++) if (bytes[k + 1] !== HEADER[k]) return null;
  return bytes.slice(4, -1);
}

// ---------- messages to the G6 ----------

export const MSG = Object.freeze({
  editorMode: [0x50],
  enterPcMode: [0x52],
  exitPcMode: [0x53],
  hostAck: [0x00, 0x00],
  requestActivePatch: [0x64, 0x13],
  requestActiveSlot: [0x33],
  requestRhythmState: [0x64, 0x0a],
  rhythmStart: [0x64, 0x20, 0x00, 0x5b, 0x00, 0x01, 0, 0, 0, 0],
  rhythmStop: [0x64, 0x20, 0x00, 0x5b, 0x01, 0x01, 0, 0, 0, 0],
  requestSdSpace: [0x64, 0x3c],
  requestWavCount: [0x64, 0x35],
  // Looper block 5C. Parameter 0 is the main foot-switch gesture: it plays a loaded, stopped loop, but can record
  // (empty Looper) or overdub (while playing). Send only through the gated flow (load, confirmation, one shot).
  looperPlay: [0x64, 0x20, 0x00, 0x5c, 0x00, 0x01, 0, 0, 0, 0],
  looperStop: [0x64, 0x20, 0x00, 0x5c, 0x01, 0x01, 0, 0, 0, 0],
});

export const WAV_LIST_MAX_RECORDS = 50; // per request
export const WAV_MAX_FILES = 256;

/** WAV list request for the inclusive range start..end (read the count first: end must be < count). */
export function wavListMsg(start, end, knownCount) {
  const ints = [start, end, knownCount].every(Number.isInteger);
  if (!ints || knownCount < 1 || knownCount > WAV_MAX_FILES || start < 0 || start > end || end >= knownCount) {
    throw new RangeError('Invalid WAV list range');
  }
  if (end - start + 1 > WAV_LIST_MAX_RECORDS) throw new RangeError(`At most ${WAV_LIST_MAX_RECORDS} records per request`);
  return [0x64, 0x37, start & 0x7f, start >> 7, end & 0x7f, end >> 7];
}

/**
 * WAV load into the Looper (64 39 LL HH). Not a read: it replaces the Looper session and may change the global BPM.
 * The device loads from its own folder scan, so resolve the index by name from a fresh list right before sending.
 */
export function wavLoadMsg(index, knownCount) {
  const ints = [index, knownCount].every(Number.isInteger);
  if (!ints || knownCount < 1 || knownCount > WAV_MAX_FILES || index < 0 || index >= knownCount) {
    throw new RangeError('Invalid WAV index');
  }
  return [0x64, 0x39, index & 0x7f, index >> 7];
}

/** Index of a file name in a WAV list (case-insensitive, as on the SD card). Throws when missing or ambiguous. */
export function findWavIndex(names, wanted) {
  if (typeof wanted !== 'string' || !wanted.trim() || wanted.includes('\0') || wanted.length > 63) {
    throw new Error('Invalid file name');
  }
  const key = wanted.trim().toLowerCase();
  const hits = names.flatMap((name, i) => (name.toLowerCase() === key ? [i] : []));
  if (!hits.length) throw new Error(`"${wanted}" is not in the WAV list`);
  if (hits.length > 1) throw new Error(`"${wanted}" matches ${hits.length} entries: ambiguous`);
  return hits[0];
}

// ---------- Looper settings (system block 64) ----------
// STOP MODE: setter 64 20 00 64 5E <val5> and readback 64 09 byte 45, verified on the device for 0 INSTANT,
// 1 FINISH and 2 FADE OUT (setting only: not the audio behaviour of a stop).
// POSITION: setter 64 20 00 64 61 <val5> and readback byte 48, verified on the device for 0 PRE-EFFECTS and
// 1 POST-EFFECTS. Changing it stops and resets the Looper, even when the value is the same: never resend it.
// LOOP MODE (byte 49) is parsed but not shown: with an SD card inserted the Looper is fixed to STEREO.

export const STOP_MODE_PARAM = 0x5e;
export const LOOPER_VOLUME_PARAM = 0x5f; // Looper volume 0-100: setter and readback (byte 46) verified on the device
export const LOOPER_VOLUME_MAX = 100;
export const STOP_MODES = Object.freeze({ 0: 'INSTANT', 1: 'FINISH', 2: 'FADE OUT' });
export const POSITION_PARAM = 0x61;
export const LOOPER_POSITIONS = Object.freeze({ 0: 'PRE-EFFECTS', 1: 'POST-EFFECTS' });
const LOOP_MODES = Object.freeze({ 0: 'MONO', 1: 'STEREO (not verified)' });

export function setStopModeMsg(mode) {
  if (!Number.isInteger(mode) || !(mode in STOP_MODES)) throw new RangeError(`Invalid STOP MODE: ${mode}`);
  return [0x64, 0x20, 0x00, SYSTEM_BLOCK, STOP_MODE_PARAM, ...val5(mode)];
}

export function setPositionMsg(position) {
  if (!Number.isInteger(position) || !(position in LOOPER_POSITIONS)) throw new RangeError(`Invalid POSITION: ${position}`);
  return [0x64, 0x20, 0x00, SYSTEM_BLOCK, POSITION_PARAM, ...val5(position)];
}

/** 64 20 0x 64 61 <val5> → { value, confirmed }, or null. */
export function parsePositionChange(b) {
  if (!b || b.length < 10 || b[0] !== 0x64 || b[1] !== 0x20 || b[2] > 1 || b[3] !== SYSTEM_BLOCK || b[4] !== POSITION_PARAM) {
    return null;
  }
  return { value: decode7(b.slice(5, 10)), confirmed: b[2] === 0x01 };
}

export function setLooperVolumeMsg(volume) {
  if (!Number.isInteger(volume) || volume < 0 || volume > LOOPER_VOLUME_MAX) throw new RangeError(`Invalid Looper volume: ${volume}`);
  return [0x64, 0x20, 0x00, SYSTEM_BLOCK, LOOPER_VOLUME_PARAM, ...val5(volume)];
}

/** 64 20 0x 64 5F <val5> → { value, confirmed }, or null. */
export function parseLooperVolumeChange(b) {
  if (!b || b.length < 10 || b[0] !== 0x64 || b[1] !== 0x20 || b[2] > 1 || b[3] !== SYSTEM_BLOCK || b[4] !== LOOPER_VOLUME_PARAM) {
    return null;
  }
  return { value: decode7(b.slice(5, 10)), confirmed: b[2] === 0x01 };
}

/** 64 20 0x 64 5E <val5> → { value, confirmed }, or null. */
export function parseStopModeChange(b) {
  if (!b || b.length < 10 || b[0] !== 0x64 || b[1] !== 0x20 || b[2] > 1 || b[3] !== SYSTEM_BLOCK || b[4] !== STOP_MODE_PARAM) {
    return null;
  }
  return { value: decode7(b.slice(5, 10)), confirmed: b[2] === 0x01 };
}

/** Looper settings from the full 64 09 frame: STOP MODE 45, volume 46, POSITION 48, LOOP MODE 49. Not transport. */
export function parseLooperSettings(frame) {
  if (!isRhythmStateFrame(frame)) throw new Error('Invalid rhythm state frame');
  const [stop, position, loop] = [frame[45], frame[48], frame[49]];
  return {
    stopMode: stop,
    stopModeName: STOP_MODES[stop] ?? `unknown (${stop})`,
    position,
    positionName: LOOPER_POSITIONS[position] ?? `unknown (${position})`,
    loopMode: loop,
    loopModeName: LOOP_MODES[loop] ?? `not qualified (${loop})`,
    volume: frame[46],
  };
}

/** Setter for a rhythm parameter (bpm, pattern, volume, count) on the system block. */
export function setRhythmParamMsg(name, value) {
  if (!isValidRhythmValue(name, value)) throw new RangeError(`Invalid ${name}: ${value}`);
  return [0x64, 0x20, 0x00, SYSTEM_BLOCK, RHYTHM_PARAMS[name].param, ...val5(value)];
}

export function readStoredPatchMsg(slot) {
  if (!isValidSlot(slot)) throw new RangeError(`Slot out of range: ${slot}`);
  const { bank, pos } = slotToBankPos(slot);
  return [0x46, 0x00, 0x00, bank, 0x00, pos, 0x00];
}

/** Standard MIDI patch change on channel 1: CC0 = 0, CC32 = bank, Program Change = position. */
export function patchSelectMessages(slot) {
  if (!isValidSlot(slot)) throw new RangeError(`Slot out of range: ${slot}`);
  const { bank, pos } = slotToBankPos(slot);
  return [
    [0xb0, 0, 0],
    [0xb0, 32, bank],
    [0xc0, pos],
  ];
}

// ---------- safety allowlist ----------

const sameBytes = (a, b) => a.length === b.length && a.every((x, k) => x === b[k]);

const FIXED_MESSAGES = [
  MSG.editorMode,
  MSG.enterPcMode,
  MSG.exitPcMode,
  MSG.hostAck,
  MSG.requestActivePatch,
  MSG.requestActiveSlot,
  MSG.requestRhythmState,
  MSG.rhythmStart,
  MSG.rhythmStop,
  MSG.requestSdSpace,
  MSG.requestWavCount,
  MSG.looperPlay,
  MSG.looperStop,
];

function isWavListRequest(b) {
  if (b.length !== 6 || b[0] !== 0x64 || b[1] !== 0x37 || b.slice(2).some((x) => x > 0x7f)) return false;
  const start = b[2] | (b[3] << 7);
  const end = b[4] | (b[5] << 7);
  return start <= end && end < WAV_MAX_FILES && end - start + 1 <= WAV_LIST_MAX_RECORDS;
}

function isStopModeSetter(b) {
  return (
    b.length === 10 &&
    sameBytes(b.slice(0, 5), [0x64, 0x20, 0x00, SYSTEM_BLOCK, STOP_MODE_PARAM]) &&
    b.slice(5).every((x) => x < 0x80) &&
    decode7(b.slice(5)) in STOP_MODES
  );
}

function isLooperVolumeSetter(b) {
  return (
    b.length === 10 &&
    sameBytes(b.slice(0, 5), [0x64, 0x20, 0x00, SYSTEM_BLOCK, LOOPER_VOLUME_PARAM]) &&
    b.slice(5).every((x) => x < 0x80) &&
    decode7(b.slice(5)) <= LOOPER_VOLUME_MAX
  );
}

function isPositionSetter(b) {
  return (
    b.length === 10 &&
    sameBytes(b.slice(0, 5), [0x64, 0x20, 0x00, SYSTEM_BLOCK, POSITION_PARAM]) &&
    b.slice(5).every((x) => x < 0x80) &&
    decode7(b.slice(5)) in LOOPER_POSITIONS
  );
}

function isWavLoadRequest(b) {
  return b.length === 4 && b[0] === 0x64 && b[1] === 0x39 && b[2] <= 0x7f && b[3] <= 0x7f && (b[2] | (b[3] << 7)) < WAV_MAX_FILES;
}

function rhythmParamName(param) {
  const entry = Object.entries(RHYTHM_PARAMS).find(([, spec]) => spec.param === param);
  return entry ? entry[0] : null;
}

function isRhythmSetter(b) {
  if (b.length !== 10 || !sameBytes(b.slice(0, 4), [0x64, 0x20, 0x00, SYSTEM_BLOCK])) return false;
  const name = rhythmParamName(b[4]);
  return name !== null && b.slice(5).every((x) => x < 0x80) && isValidRhythmValue(name, decode7(b.slice(5)));
}

function isStoredPatchRead(b) {
  return b.length === 7 && b[0] === 0x46 && b[1] === 0 && b[2] === 0 && b[3] < 60 && b[4] === 0 && b[5] < 4 && b[6] === 0;
}

/** Throws unless the SysEx body is one of the verified messages. */
export function assertSafeToSend(body) {
  const allowed =
    FIXED_MESSAGES.some((m) => sameBytes(body, m)) ||
    isRhythmSetter(body) ||
    isStoredPatchRead(body) ||
    isWavListRequest(body) ||
    isWavLoadRequest(body) ||
    isStopModeSetter(body) ||
    isLooperVolumeSetter(body) ||
    isPositionSetter(body);
  if (!allowed) {
    const hex = body.map((x) => x.toString(16).padStart(2, '0')).join(' ');
    throw new Error(`Blocked: message not in the verified list (${hex})`);
  }
}

/** Throws unless the channel message is CC0, CC32 or Program Change on channel 1. */
export function assertSafeChannelMessage(bytes) {
  const ok =
    (bytes.length === 3 && bytes[0] === 0xb0 && (bytes[1] === 0 || bytes[1] === 32) && bytes[2] < 128) ||
    (bytes.length === 2 && bytes[0] === 0xc0 && bytes[1] < 4);
  if (!ok) throw new Error('Blocked: channel message not in the verified list');
}

// ---------- parsing replies and notifications ----------

export const isOk = (b) => b.length >= 2 && b[0] === 0x00 && b[1] === 0x00;

export const isActivePatchDump = (b) => b[0] === 0x64 && b[1] === 0x12;

/** Name from the reply to requestActivePatch: 64 12 01 28 06 <packed PTCF> <CRC 5 bytes>. */
export function parseActivePatchName(b) {
  return ptcfName(unpack7(b.slice(5, -5)));
}

export function isStoredPatchDump(b, slot) {
  const { bank, pos } = slotToBankPos(slot);
  return b[0] === 0x45 && b[3] === bank && b[5] === pos;
}

/** Name from a stored patch read: 45 00 00 BB 00 PP 00 28 06 <packed PTCF> <CRC 5 bytes>. */
export function parseStoredPatchName(b) {
  return ptcfName(unpack7(b.slice(9, -5)));
}

export const EMPTY_PATCH_NAME = 'Empty';

/** Footswitch patch change notification: 64 26 00 00 BB 00 PP 00 → slot, or null. */
export function parsePatchChanged(b) {
  if (b.length < 7 || b[0] !== 0x64 || b[1] !== 0x26) return null;
  return bankPosToSlot(b[4], b[6]);
}

export const RHYTHM_STATE_LENGTH = 61;

/** True for a full 64 09 frame of the expected length. */
export function isRhythmStateFrame(frame) {
  const b = bodyOf(frame);
  return b !== null && frame.length === RHYTHM_STATE_LENGTH && b[0] === 0x64 && b[1] === 0x09;
}

/**
 * Rhythm state from the full 64 09 frame (offsets are zero-based on the complete SysEx, F0 included).
 * Playback state is not part of the reply: it is always reported as unknown.
 */
export function parseRhythmState(frame) {
  if (!isRhythmStateFrame(frame)) throw new Error('Invalid rhythm state frame');
  return {
    bpm: decode7(frame.slice(6, 8)),
    pattern: frame[41],
    volume: frame[42],
    count: frame[43],
    playing: null,
    playingVerified: false,
  };
}

/**
 * Rhythm parameter change: 64 20 0x 64 PP <val5> → { name, value, confirmed }, or null.
 * Third byte 01 = confirmation of a setter.
 */
export function parseRhythmParam(b) {
  if (b.length < 10 || b[0] !== 0x64 || b[1] !== 0x20 || b[2] > 1 || b[3] !== SYSTEM_BLOCK) return null;
  const name = rhythmParamName(b[4]);
  return name ? { name, value: decode7(b.slice(5, 10)), confirmed: b[2] === 0x01 } : null;
}

// ---------- SD card and WAV files (read-only) ----------

/** Decode N groups of 7 bits, LSB first, refusing values beyond the safe integer range. */
export function decode7Wide(bytes) {
  let value = 0;
  bytes.forEach((x, k) => {
    value += (x & 0x7f) * 2 ** (7 * k);
  });
  if (!Number.isSafeInteger(value)) throw new Error('Value out of range');
  return value;
}

function isReplyFrame(frame, command, length) {
  const b = bodyOf(frame);
  return b !== null && b[0] === 0x64 && b[1] === command && (length === undefined || frame.length === length);
}

export const isSdSpaceFrame = (frame) => isReplyFrame(frame, 0x3d, 27);
export const isWavCountFrame = (frame) => isReplyFrame(frame, 0x36, 9);
export const isWavListFrame = (frame) => isReplyFrame(frame, 0x38) && frame.length === 8 + 64 * frame[6];

/** 64 3D: total at offsets 6-15, free at 16-25 (10 × 7 bits each, bytes). */
export function parseSdSpace(frame) {
  if (!isSdSpaceFrame(frame)) throw new Error('Invalid SD space frame');
  const totalBytes = decode7Wide(frame.slice(6, 16));
  const freeBytes = decode7Wide(frame.slice(16, 26));
  if (freeBytes > totalBytes) throw new Error('SD free space exceeds total');
  return { totalBytes, freeBytes };
}

/** 64 36: number of WAV files in the device cache, offsets 6-7. */
export function parseWavCount(frame) {
  if (!isWavCountFrame(frame)) throw new Error('Invalid WAV count frame');
  const count = decode7Wide(frame.slice(6, 8));
  if (count > WAV_MAX_FILES) throw new Error('WAV count out of range');
  return count;
}

/** 64 38: record count at offset 6, then 64-byte NUL-padded names. Checks the count against the requested range. */
export function parseWavList(frame, start, end) {
  if (!isWavListFrame(frame)) throw new Error('Invalid WAV list frame');
  const count = frame[6];
  if (count !== end - start + 1) throw new Error('WAV list does not match the requested range: the cache may have changed');
  const names = [];
  for (let i = 0; i < count; i++) {
    const record = frame.slice(7 + 64 * i, 7 + 64 * (i + 1));
    const nul = record.indexOf(0);
    names.push(String.fromCharCode(...(nul === -1 ? record : record.slice(0, nul))));
  }
  return names;
}

/** True when the body confirms the setter for `name`. */
export function isRhythmConfirmation(b, name) {
  const parsed = parseRhythmParam(b);
  return Boolean(parsed && parsed.confirmed && parsed.name === name);
}

// ---------- WAV load replies ----------

/**
 * Result codes of the generic reply 00 RR to a WAV load. Only 00 was seen on a real G6; other values are not
 * documented. The reply has no opcode or index, and 00 does not prove the copy is correct.
 */
export const WAV_LOAD_RESULTS = Object.freeze({
  0x00: 'completed',
});

/** Result code of a generic reply 00 RR, or null. */
export const ackResult = (b) => (b && b.length === 2 && b[0] === 0x00 ? b[1] : null);

/** Looper notification 64 3B + 64 ASCII bytes: the current file name, or null. */
export function parseLooperFileEvent(b) {
  if (!b || b.length !== 66 || b[0] !== 0x64 || b[1] !== 0x3b) return null;
  const record = b.slice(2, 66);
  const nul = record.indexOf(0);
  return String.fromCharCode(...(nul === -1 ? record : record.slice(0, nul)));
}
