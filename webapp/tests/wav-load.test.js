// WAV load action against a simulated G6 behind the real Web MIDI transport: `npm test` (Node 20+). No MIDI port.
import { test, beforeEach } from 'node:test';
import assert from 'node:assert/strict';
import { FakeG6, page, resetStorage } from './fake-g6.js';

beforeEach(resetStorage);

test('one reply is reported without certainty, with the announced name', async () => {
  const g6 = new FakeG6();
  const { load } = page(g6);
  const state = await load('looper_001.wav');
  assert.equal(state.error, null);
  assert.match(state.notice, /One generic reply: 0x00 \(completed\)\. It cannot be tied to this load with certainty/);
  assert.match(state.notice, /Announced: "LOOPER_001.WAV"/);
  assert.equal(state.storage.loadUncertain, null);
  assert.deepEqual(g6.loads().map((d) => d.slice(4, -1)), [[0x64, 0x39, 1, 0]]);
});

test('missing 64 3B: "No file name announced"', async () => {
  const g6 = new FakeG6();
  g6.announce = false;
  const { load } = page(g6);
  const state = await load('LOOPER_001.WAV');
  assert.match(state.notice, /No file name announced by the G6\./);
});

test('an error code is not presented as a certain refusal', async () => {
  const g6 = new FakeG6();
  g6.loadResult = 0x13;
  const { load } = page(g6);
  const state = await load('LOOPER_001.WAV');
  assert.match(state.notice, /One generic reply: 0x13 \(undocumented code\)\. It cannot be tied/);
  assert.doesNotMatch(state.notice, /refused/i);
});

test('timeout blocks further loads until the user checks the Looper', async () => {
  const g6 = new FakeG6();
  g6.loadResult = null;
  const { actions, store, load } = page(g6);
  let state = await load('LOOPER_001.WAV');
  assert.match(state.notice, /No generic reply within 0.15 s: not resent/);
  assert.equal(state.storage.loadUncertain.name, 'LOOPER_001.WAV');

  const before = g6.sent.length;
  state = await load('LOOPER_002.WAV');
  assert.match(state.error, /uncertain outcome/);
  assert.equal(g6.sent.length, before); // nothing sent, not even reads

  await actions.execute('storage.clearUncertainLoad', false);
  assert.match(store.get().error, /Check the Looper screen first/);
  await actions.execute('storage.clearUncertainLoad', true);
  assert.equal(store.get().storage.loadUncertain, null);
  assert.match(store.get().notice, /late reply to that load can still arrive/);
  assert.equal(g6.sent.length, before); // recovery sends nothing
});

test('a late reply after a timeout is not taken as the result of the next load', async () => {
  const g6 = new FakeG6();
  g6.loadResult = null;
  g6.holdReply = 0x16; // the first load answers only after its window closed
  const { actions, load } = page(g6);
  await load('LOOPER_001.WAV');
  await actions.execute('storage.clearUncertainLoad', true);
  g6.loadResult = 0x00;
  const state = await load('LOOPER_002.WAV'); // receives the late 0x16, then its own 0x00
  assert.match(state.notice, /2 generic replies arrived \(0x16, 0x00\)/);
  assert.match(state.notice, /Outcome uncertain/);
  assert.doesNotMatch(state.notice, /One generic reply/);
  assert.equal(state.storage.loadUncertain.name, 'LOOPER_002.WAV');
  assert.equal(g6.loads().length, 2);
});

test('the uncertain state survives a page reload', async () => {
  const g6 = new FakeG6();
  g6.loadResult = null;
  await page(g6).load('LOOPER_001.WAV');
  const reloaded = page(g6);
  assert.equal(reloaded.store.get().storage.loadUncertain.name, 'LOOPER_001.WAV');
  const state = await reloaded.load('LOOPER_002.WAV');
  assert.match(state.error, /uncertain outcome/);
  assert.equal(g6.loads().length, 1);
});

test('without confirmation nothing is sent', async () => {
  const g6 = new FakeG6();
  const { actions, store } = page(g6);
  await actions.execute('storage.loadWav', 'LOOPER_001.WAV', false);
  assert.match(store.get().error, /Confirm that the Looper is stopped/);
  assert.equal(g6.sent.length, 0);
});
