// Simulated G6 behind the real Web MIDI transport, shared by the action tests. No MIDI port is opened.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { G6Midi } from '../public/js/midi.js';
import { G6Actions } from '../public/js/actions.js';
import { createStore, initialState } from '../public/js/store.js';

const vectors = JSON.parse(readFileSync(new URL('../../protocol/test-vectors.json', import.meta.url)));
const bytes = (hex) => hex.split(' ').map((x) => parseInt(x, 16));

// Minimal localStorage, shared by the "pages" of a test to check persistence.
const storage = {};
export const resetStorage = () => Object.keys(storage).forEach((k) => delete storage[k]);
globalThis.localStorage = {
  getItem: (k) => (k in storage ? storage[k] : null),
  setItem: (k, v) => (storage[k] = String(v)),
  removeItem: (k) => delete storage[k],
};

export class FakeG6 {
  constructor() {
    this.cache = ['hey joe.wav', 'LOOPER_001.WAV', 'LOOPER_002.WAV'];
    this.loadResult = 0x00; // null = no reply in time
    this.holdReply = null; // with loadResult null: a reply that arrives later, at the next 64 39
    this.lateReplies = [];
    this.announce = true;
    this.sent = [];
    this.looper = []; // play/stop gestures received
    this.stopMode = 0;
    this.looperVolume = 60;
    this.position = 1;
    this.silentPosition = false;
    this.silentLooperVolume = false;
    this.silentStopMode = false;
    this.loaded = null;
    this.input = { name: 'ZOOM G6', state: 'connected', onmidimessage: null };
    this.output = { name: 'ZOOM G6', state: 'connected', send: (data) => this.receive(Array.from(data)) };
  }

  emit(frame) {
    setTimeout(() => this.input.onmidimessage?.({ data: Uint8Array.from(frame) }), 1);
  }

  sysex(body) {
    this.emit([0xf0, 0x52, 0x00, 0x6e, ...body, 0xf7]);
  }

  receive(data) {
    this.sent.push(data);
    const b = data.slice(4, -1);
    const pad = (s) => [...s].map((c) => c.charCodeAt(0)).concat(Array(64 - s.length).fill(0));
    if (b[0] === 0x64 && b[1] === 0x3c) this.emit(bytes(vectors.storage.sd_space.frame));
    else if (b[0] === 0x64 && b[1] === 0x35) this.sysex([0x64, 0x36, this.cache.length, 0]);
    else if (b[0] === 0x64 && b[1] === 0x37) {
      const s = b[2] | (b[3] << 7);
      const e = b[4] | (b[5] << 7);
      const body = [0x64, 0x38, e - s + 1];
      for (let i = s; i <= e; i++) body.push(...pad(this.cache[i]));
      this.sysex(body);
    } else if (b[0] === 0x64 && b[1] === 0x0a) {
      const frame = bytes(vectors.rhythm_states[0].frame);
      frame[45] = this.stopMode;
      frame[46] = this.looperVolume;
      frame[48] = this.position;
      this.emit(frame);
    } else if (b[0] === 0x64 && b[1] === 0x20 && b[3] === 0x64 && b[4] === 0x5f) {
      this.looperVolume = b[5];
      if (!this.silentLooperVolume) this.sysex([0x64, 0x20, 0x01, ...b.slice(3)]);
    } else if (b[0] === 0x64 && b[1] === 0x20 && b[3] === 0x64 && b[4] === 0x61) {
      this.position = b[5];
      if (!this.silentPosition) this.sysex([0x64, 0x20, 0x01, ...b.slice(3)]);
    } else if (b[0] === 0x64 && b[1] === 0x20 && b[3] === 0x64 && b[4] === 0x5e) {
      this.stopMode = b[5];
      if (!this.silentStopMode) this.sysex([0x64, 0x20, 0x01, ...b.slice(3)]);
    }
    else if (b[0] === 0x64 && b[1] === 0x39) {
      for (const r of this.lateReplies.splice(0)) this.sysex([0x00, r]);
      if (this.loadResult === null) {
        if (this.holdReply !== null) this.lateReplies.push(this.holdReply);
        return;
      }
      this.loaded = this.cache[b[2] | (b[3] << 7)];
      if (this.announce) this.sysex([0x64, 0x3b, ...pad(this.loaded)]);
      this.sysex([0x00, this.loadResult]);
    } else if (b[0] === 0x64 && b[1] === 0x20 && b[3] === 0x5c) {
      this.looper.push(b[4] === 0 ? 'play' : 'stop');
      this.footSwitch();
    }
  }

  /** A Looper event as sent when a foot switch is pressed: 64 3B with the current file name. */
  footSwitch() {
    const pad = (s) => [...s].map((c) => c.charCodeAt(0)).concat(Array(64 - s.length).fill(0));
    this.sysex([0x64, 0x3b, ...pad(this.loaded ?? 'New Loop File')]);
  }

  loads() {
    return this.sent.filter((d) => d[4] === 0x64 && d[5] === 0x39);
  }
}

export function page(g6) {
  const midi = new G6Midi();
  midi.access = { inputs: new Map([['in', g6.input]]), outputs: new Map([['out', g6.output]]) };
  assert.equal(midi.attach(), true);
  const store = createStore(initialState);
  store.set({ connection: 'connected' });
  const actions = new G6Actions(midi, store);
  actions.wavLoadTimeoutMs = 150;
  actions.wavLoadLingerMs = 30;
  actions.looperListenMs = 20;
  const load = (name) => actions.execute('storage.loadWav', name, true).then(() => store.get());
  return { actions, store, load };
}

