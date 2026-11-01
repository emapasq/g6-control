// Gated Looper play/stop against a simulated G6: `npm test` (Node 20+). No MIDI port.
import { test, beforeEach } from 'node:test';
import assert from 'node:assert/strict';
import { FakeG6, page, resetStorage } from './fake-g6.js';

beforeEach(resetStorage);

const gestures = (g6) => g6.sent.filter((d) => d[4] === 0x64 && d[5] === 0x20 && d[7] === 0x5c).map((d) => d.slice(4, -1));
const PLAY = [0x64, 0x20, 0x00, 0x5c, 0x00, 0x01, 0, 0, 0, 0];
const STOP = [0x64, 0x20, 0x00, 0x5c, 0x01, 0x01, 0, 0, 0, 0];

test('play needs a load from this page and a confirmation', async () => {
  const g6 = new FakeG6();
  const { actions, store, load } = page(g6);
  await actions.execute('looper.play');
  assert.match(store.get().error, /Play is not allowed/);
  await actions.execute('looper.arm', true);
  assert.match(store.get().error, /Load a WAV first/);
  await load('LOOPER_001.WAV');
  await actions.execute('looper.arm', false);
  assert.equal(store.get().looper.armed, false);
  await actions.execute('looper.stop');
  assert.match(store.get().error, /Stop is available only after a play/);
  assert.deepEqual(gestures(g6), []);
});

test('one play, one stop, then a new confirmation is needed', async () => {
  const g6 = new FakeG6();
  const { actions, store, load } = page(g6);
  await load('LOOPER_002.WAV');
  await actions.execute('looper.arm', true);
  assert.equal(store.get().looper.armed, true);
  await actions.execute('looper.play');
  assert.match(store.get().notice, /Play sent once \(Looper with "LOOPER_002.WAV"\)/);
  assert.match(store.get().notice, /Looper notification: LOOPER_002.WAV/);
  assert.equal(store.get().looper.armed, false);
  assert.equal(store.get().looper.stopAvailable, true);

  await actions.execute('looper.play'); // one shot
  assert.match(store.get().error, /Play is not allowed/);
  await actions.execute('looper.stop');
  assert.match(store.get().notice, /Stop sent once/);
  await actions.execute('looper.stop'); // one shot
  assert.match(store.get().error, /Stop is available only after a play/);
  await actions.execute('looper.play'); // no automatic re-enable
  assert.match(store.get().error, /Play is not allowed/);
  assert.deepEqual(gestures(g6), [PLAY, STOP]);
});

test('a Looper event from the device withdraws the play permission', async () => {
  const g6 = new FakeG6();
  const { actions, store, load } = page(g6);
  await load('LOOPER_001.WAV');
  await actions.execute('looper.arm', true);
  g6.footSwitch();
  await new Promise((resolve) => setTimeout(resolve, 20));
  assert.equal(store.get().looper.armed, false);
  assert.match(store.get().notice, /play permission withdrawn/);
  await actions.execute('looper.play');
  assert.match(store.get().error, /Play is not allowed/);
  assert.deepEqual(gestures(g6), []);
});

test('an uncertain load cannot be armed, and a new load withdraws the permission', async () => {
  const g6 = new FakeG6();
  const { actions, store, load } = page(g6);
  await load('LOOPER_001.WAV');
  await actions.execute('looper.arm', true);
  g6.loadResult = null;
  await load('LOOPER_002.WAV'); // new load: permission gone, outcome uncertain
  assert.equal(store.get().looper.armed, false);
  assert.equal(store.get().looper.loaded, null);
  await actions.execute('looper.arm', true);
  assert.match(store.get().error, /uncertain outcome/);
});

test('after a reload play needs a new confirmation, stop stays available', async () => {
  const g6 = new FakeG6();
  const first = page(g6);
  await first.load('LOOPER_001.WAV');
  await first.actions.execute('looper.arm', true);
  const reloaded = page(g6);
  assert.equal(reloaded.store.get().looper.armed, false); // never restored
  assert.equal(reloaded.store.get().looper.loaded.name, 'LOOPER_001.WAV');

  await reloaded.actions.execute('looper.arm', true);
  await reloaded.actions.execute('looper.play');
  const again = page(g6);
  assert.equal(again.store.get().looper.stopAvailable, true);
  await again.actions.execute('looper.stop');
  assert.deepEqual(gestures(g6), [PLAY, STOP]);
});

test('the play permission is used up even if sending fails', async () => {
  const g6 = new FakeG6();
  const { actions, store, load } = page(g6);
  await load('LOOPER_001.WAV');
  await actions.execute('looper.arm', true);
  g6.output.send = () => {
    throw new Error('port gone');
  };
  await actions.execute('looper.play');
  assert.match(store.get().error, /port gone/);
  assert.equal(store.get().looper.armed, false);
  assert.equal(store.get().looper.stopAvailable, true);
});

const stopModeSetters = (g6) => g6.sent.filter((d) => d[5] === 0x20 && d[7] === 0x64 && d[8] === 0x5e).map((d) => d.slice(4, -1));

test('STOP MODE: one setter, confirmation, read back; never a stop', async () => {
  const g6 = new FakeG6();
  const { actions, store } = page(g6);
  await actions.execute('rhythm.refresh');
  assert.equal(store.get().looperSettings.stopModeName, 'INSTANT');
  assert.equal(store.get().looperSettings.positionName, 'POST-EFFECTS');
  assert.equal(store.get().looperSettings.loopModeName, 'MONO');
  await actions.execute('looper.setStopMode', 1);
  assert.equal(store.get().error, null);
  assert.equal(store.get().looperSettings.stopModeName, 'FINISH');
  assert.deepEqual(stopModeSetters(g6), [[0x64, 0x20, 0x00, 0x64, 0x5e, 1, 0, 0, 0, 0]]);
  assert.deepEqual(gestures(g6), []);
});

test('STOP MODE: invalid values and unconfirmed setter are not resent', async () => {
  const g6 = new FakeG6();
  const { actions, store } = page(g6);
  await actions.execute('looper.setStopMode', 3);
  assert.match(store.get().error, /0, 1 or 2/);
  assert.equal(g6.sent.length, 0);
  g6.silentStopMode = true;
  await actions.execute('looper.setStopMode', 2);
  assert.match(store.get().notice, /not confirmed by the G6 \(not resent\)/);
  assert.equal(stopModeSetters(g6).length, 1);
});

test('STOP MODE cannot be changed while a play sent by this page is running', async () => {
  const g6 = new FakeG6();
  const { actions, store, load } = page(g6);
  await load('LOOPER_001.WAV');
  await actions.execute('looper.arm', true);
  await actions.execute('looper.play');
  await actions.execute('looper.setStopMode', 2);
  assert.match(store.get().error, /Stop the play sent by this page first/);
  assert.equal(stopModeSetters(g6).length, 0);
});

const volumeSetters = (g6) => g6.sent.filter((d) => d[5] === 0x20 && d[7] === 0x64 && d[8] === 0x5f).map((d) => d.slice(4, -1));

test('Looper volume: one setter, confirmation, read back', async () => {
  const g6 = new FakeG6();
  const { actions, store } = page(g6);
  await actions.execute('rhythm.refresh');
  assert.equal(store.get().looperSettings.volume, 60);
  await actions.execute('looper.setVolume', 50);
  assert.equal(store.get().error, null);
  assert.equal(store.get().looperSettings.volume, 50);
  assert.deepEqual(volumeSetters(g6), [[0x64, 0x20, 0x00, 0x64, 0x5f, 50, 0, 0, 0, 0]]);
  await actions.execute('looper.setVolume', 101);
  assert.match(store.get().error, /between 0 and 100/);
  g6.silentLooperVolume = true;
  await actions.execute('looper.setVolume', 40);
  assert.match(store.get().notice, /not confirmed by the G6 \(not resent\)/);
  assert.equal(volumeSetters(g6).length, 2);
  assert.deepEqual(gestures(g6), []);
});

const positionSetters = (g6) => g6.sent.filter((d) => d[5] === 0x20 && d[7] === 0x64 && d[8] === 0x61).map((d) => d.slice(4, -1));

test('POSITION: needs the confirmation and a valid value', async () => {
  const g6 = new FakeG6();
  const { actions, store } = page(g6);
  await actions.execute('looper.setPosition', 0, false);
  assert.match(store.get().error, /stops and resets the Looper/);
  await actions.execute('looper.setPosition', 2, true);
  assert.match(store.get().error, /0 or 1/);
  assert.equal(g6.sent.length, 0);
});

test('POSITION: one setter, read back, the Looper state is dropped; same value is not resent', async () => {
  const g6 = new FakeG6();
  const { actions, store, load } = page(g6);
  await load('LOOPER_001.WAV');
  await actions.execute('looper.arm', true);
  await actions.execute('looper.setPosition', 0, true);
  assert.equal(store.get().error, null);
  assert.match(store.get().notice, /POSITION PRE-EFFECTS: confirmed\. The Looper was reset/);
  assert.equal(store.get().looperSettings.positionName, 'PRE-EFFECTS');
  assert.equal(store.get().looper.loaded, null);
  assert.equal(store.get().looper.armed, false);
  assert.deepEqual(positionSetters(g6), [[0x64, 0x20, 0x00, 0x64, 0x61, 0, 0, 0, 0, 0]]);
  await actions.execute('looper.setPosition', 0, true);
  assert.match(store.get().notice, /already PRE-EFFECTS: nothing sent/);
  assert.equal(positionSetters(g6).length, 1);
  assert.deepEqual(gestures(g6), []);
});

test('POSITION: unconfirmed setter is not resent; refused while a play is running', async () => {
  const g6 = new FakeG6();
  const { actions, store, load } = page(g6);
  g6.silentPosition = true;
  await actions.execute('looper.setPosition', 0, true);
  assert.match(store.get().notice, /not confirmed by the G6 \(not resent\)/);
  assert.equal(positionSetters(g6).length, 1);
  await load('LOOPER_001.WAV');
  await actions.execute('looper.arm', true);
  await actions.execute('looper.play');
  await actions.execute('looper.setPosition', 1, true);
  assert.match(store.get().error, /Stop the play sent by this page first/);
  assert.equal(positionSetters(g6).length, 1);
});
