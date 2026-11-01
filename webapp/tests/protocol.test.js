// Runs the shared protocol test vectors against the web app codec: `npm test` (Node 20+).
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import * as P from '../public/js/g6-protocol.js';

const vectors = JSON.parse(readFileSync(new URL('../../protocol/test-vectors.json', import.meta.url)));
const bytes = (hex) => hex.split(' ').map((x) => parseInt(x, 16));

test('fixed requests are built and allowed', () => {
  const byId = Object.fromEntries(vectors.requests.map((v) => [v.id, bytes(v.frame)]));
  assert.deepEqual(P.toSysex(P.MSG.requestRhythmState), byId['rhythm-state-request']);
  assert.deepEqual(P.toSysex(P.MSG.rhythmStart), byId['rhythm-start']);
  assert.deepEqual(P.toSysex(P.MSG.rhythmStop), byId['rhythm-stop']);
  for (const frame of Object.values(byId)) assert.doesNotThrow(() => P.assertSafeToSend(P.bodyOf(frame)));
});

for (const v of vectors.setters) {
  test(`setter ${v.id}`, () => {
    const body = P.setRhythmParamMsg(v.param, v.value);
    assert.deepEqual(P.toSysex(body), bytes(v.frame));
    assert.doesNotThrow(() => P.assertSafeToSend(body));
  });
}

for (const v of vectors.confirmations) {
  test(`confirmation ${v.id}`, () => {
    const body = P.bodyOf(bytes(v.frame));
    assert.deepEqual(P.parseRhythmParam(body), v.expect);
    assert.equal(P.isRhythmConfirmation(body, v.expect.name), true);
  });
}

for (const v of vectors.rhythm_states) {
  test(`rhythm state ${v.id}`, () => {
    const frame = bytes(v.frame);
    assert.equal(frame.length, P.RHYTHM_STATE_LENGTH);
    assert.equal(P.isRhythmStateFrame(frame), true);
    assert.deepEqual(P.parseRhythmState(frame), v.expect);
  });
}

for (const v of vectors.notifications) {
  test(`notification ${v.id}`, () => {
    assert.equal(P.parsePatchChanged(P.bodyOf(bytes(v.frame))), v.expect.slot);
  });
}

for (const v of vectors.must_be_blocked) {
  test(`blocked: ${v.id}`, () => {
    assert.throws(() => P.assertSafeToSend(P.bodyOf(bytes(v.frame))), /Blocked/);
  });
}

test('pattern names follow the owner\'s manual numbering', () => {
  assert.equal(P.RHYTHM_PATTERNS.length, 68);
  assert.equal(P.RHYTHM_PATTERNS[0], 'GUIDE');
  assert.equal(P.RHYTHM_PATTERNS[32], 'IndiePop'); // manual No. 33
  assert.equal(P.RHYTHM_PATTERNS[67], 'Metro');
});

test('setters reject out-of-range values', () => {
  assert.throws(() => P.setRhythmParamMsg('bpm', 251), RangeError);
  assert.throws(() => P.setRhythmParamMsg('volume', -1), RangeError);
  assert.throws(() => P.setRhythmParamMsg('count', 2), RangeError);
  assert.throws(() => P.setRhythmParamMsg('unknown', 1), RangeError);
});

test('SD space reply', () => {
  const v = vectors.storage.sd_space;
  assert.deepEqual(P.parseSdSpace(bytes(v.frame)), v.expect);
});

test('WAV count reply', () => {
  const v = vectors.storage.wav_count;
  assert.equal(P.parseWavCount(bytes(v.frame)), v.expect.count);
});

test('WAV list request and reply', () => {
  const r = vectors.storage.wav_list_request;
  const body = P.wavListMsg(r.start, r.end, r.known_count);
  assert.deepEqual(P.toSysex(body), bytes(r.frame));
  assert.doesNotThrow(() => P.assertSafeToSend(body));
  const v = vectors.storage.wav_list_reply;
  const frame = bytes(v.frame);
  assert.equal(P.isWavListFrame(frame), true);
  assert.deepEqual(P.parseWavList(frame, v.start, v.end), v.expect.names);
  assert.throws(() => P.parseWavList(frame, 0, 2), /cache may have changed/);
});

test('WAV list request validation', () => {
  assert.throws(() => P.wavListMsg(0, 0, 0), RangeError); // empty or unknown cache
  assert.throws(() => P.wavListMsg(0, 45, 45), RangeError); // end beyond count
  assert.throws(() => P.wavListMsg(0, 50, 100), RangeError); // 51 records
  assert.deepEqual(P.wavListMsg(50, 99, 130), [0x64, 0x37, 50, 0, 99, 0]);
  assert.deepEqual(P.wavListMsg(200, 210, 256), [0x64, 0x37, 200 & 0x7f, 1, 210 & 0x7f, 1]);
});

test('WAV load vectors', () => {
  const w = vectors.wav_load;
  const body = P.wavLoadMsg(w.request.index, w.request.known_count);
  assert.deepEqual(P.toSysex(body), bytes(w.request.frame));
  assert.doesNotThrow(() => P.assertSafeToSend(body));
  assert.equal(P.ackResult(P.bodyOf(bytes(w.reply_ok.frame))), w.reply_ok.expect.result);
  assert.equal(P.ackResult(P.bodyOf(bytes(w.reply_error.frame))), w.reply_error.expect.result);
  assert.equal(P.parseLooperFileEvent(P.bodyOf(bytes(w.file_event.frame))), w.file_event.expect.name);
});

test('WAV load index validation', () => {
  assert.deepEqual(P.wavLoadMsg(200, 256), [0x64, 0x39, 200 & 0x7f, 1]);
  for (const [index, count] of [[45, 45], [-1, 45], [0, 0], [0, 257], [1.5, 45]]) {
    assert.throws(() => P.wavLoadMsg(index, count), RangeError);
  }
});

test('WAV name resolution', () => {
  const names = ['hey joe.wav', 'LOOPER_001.WAV'];
  assert.equal(P.findWavIndex(names, 'looper_001.wav'), 1);
  assert.throws(() => P.findWavIndex(names, 'missing.wav'), /not in the WAV list/);
  assert.throws(() => P.findWavIndex(['A.WAV', 'a.wav'], 'A.WAV'), /ambiguous/);
  for (const bad of ['', '  ', 'x'.repeat(64), null]) assert.throws(() => P.findWavIndex(names, bad));
});

test('Looper play and stop vectors are the only allowed 5C gestures', () => {
  const lp = vectors.looper;
  assert.deepEqual(P.toSysex(P.MSG.looperPlay), bytes(lp.play.frame));
  assert.deepEqual(P.toSysex(P.MSG.looperStop), bytes(lp.stop.frame));
  assert.doesNotThrow(() => P.assertSafeToSend(P.MSG.looperPlay));
  assert.doesNotThrow(() => P.assertSafeToSend(P.MSG.looperStop));
});

test('Looper settings vectors (STOP MODE and POSITION verified; LOOP MODE read-only)', () => {
  const ls = vectors.looper_settings;
  for (const v of ls.states) assert.deepEqual(P.parseLooperSettings(bytes(v.frame)), v.expect, v.id);
  for (const v of ls.setters) {
    const body = P.setStopModeMsg(v.value);
    assert.deepEqual(P.toSysex(body), bytes(v.frame), v.id);
    assert.doesNotThrow(() => P.assertSafeToSend(body));
  }
  for (const v of ls.confirmations) assert.deepEqual(P.parseStopModeChange(P.bodyOf(bytes(v.frame))), v.expect, v.id);
  for (const bad of [3, -1, 1.5, '1']) assert.throws(() => P.setStopModeMsg(bad), RangeError);
  const old = bytes(vectors.rhythm_states[0].frame);
  old[48] = 0;
  old[49] = 1;
  const s = P.parseLooperSettings(old);
  assert.equal(s.positionName, 'PRE-EFFECTS');
  assert.equal(s.loopModeName, 'STEREO (not verified)');
});

test('Looper POSITION vectors and limits', () => {
  const ls = vectors.looper_settings;
  for (const v of ls.position_setters) {
    const body = P.setPositionMsg(v.value);
    assert.deepEqual(P.toSysex(body), bytes(v.frame), v.id);
    assert.doesNotThrow(() => P.assertSafeToSend(body));
  }
  for (const v of ls.position_confirmations) assert.deepEqual(P.parsePositionChange(P.bodyOf(bytes(v.frame))), v.expect, v.id);
  for (const bad of [2, -1, 0.5, '1']) assert.throws(() => P.setPositionMsg(bad), RangeError);
  assert.equal(P.parsePositionChange([0x64, 0x20, 0x01, 0x64, 0x5e, 1, 0, 0, 0, 0]), null);
});

test('Looper volume vectors and limits', () => {
  const ls = vectors.looper_settings;
  for (const v of ls.volume_setters) {
    const body = P.setLooperVolumeMsg(v.value);
    assert.deepEqual(P.toSysex(body), bytes(v.frame), v.id);
    assert.doesNotThrow(() => P.assertSafeToSend(body));
  }
  for (const v of ls.volume_confirmations) assert.deepEqual(P.parseLooperVolumeChange(P.bodyOf(bytes(v.frame))), v.expect, v.id);
  for (const bad of [101, -1, 1.5, '50']) assert.throws(() => P.setLooperVolumeMsg(bad), RangeError);
});
