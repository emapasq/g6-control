// Web MIDI transport for the G6: port discovery, safe sending, request/response, serialisation.

import { PORT_NAME, toSysex, bodyOf, assertSafeToSend, assertSafeChannelMessage } from './g6-protocol.js';

export const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

export const toHex = (bytes) => Array.from(bytes, (x) => x.toString(16).padStart(2, '0')).join(' ');

/**
 * Events (CustomEvent.detail):
 *  - 'message' { bytes, body }   every incoming message (body = SysEx body or null)
 *  - 'ports'   { present }       G6 ports appeared / disappeared
 *  - 'log'     { dir, bytes }    'in' / 'out' traffic
 */
export class G6Midi extends EventTarget {
  constructor() {
    super();
    this.access = null;
    this.input = null;
    this.output = null;
    this.waiters = new Set();
    this.queue = Promise.resolve();
  }

  static isSupported() {
    return typeof navigator !== 'undefined' && 'requestMIDIAccess' in navigator;
  }

  async requestAccess() {
    if (this.access) return this.access;
    this.access = await navigator.requestMIDIAccess({ sysex: true });
    this.access.addEventListener('statechange', () => this.#onStateChange());
    return this.access;
  }

  findPorts() {
    const pick = (ports) =>
      [...ports.values()].find((p) => p.name && p.name.includes(PORT_NAME) && p.state === 'connected') || null;
    return { input: pick(this.access.inputs), output: pick(this.access.outputs) };
  }

  /** Attach to the G6 ports. Returns false when the G6 is not visible. */
  attach() {
    const { input, output } = this.findPorts();
    if (!input || !output) return false;
    if (this.input && this.input !== input) this.input.onmidimessage = null;
    this.input = input;
    this.output = output;
    this.input.onmidimessage = (event) => this.#onMessage(Array.from(event.data));
    return true;
  }

  get attached() {
    return Boolean(this.input && this.output && this.output.state === 'connected');
  }

  /** Wait for an incoming message matching `match(bytes)`. Resolves with the bytes, or null on timeout. */
  waitFor(match, timeoutMs = 1000) {
    return new Promise((resolve) => {
      const waiter = {
        match,
        resolve: (value) => {
          clearTimeout(waiter.timer);
          this.waiters.delete(waiter);
          resolve(value);
        },
      };
      waiter.timer = setTimeout(() => waiter.resolve(null), timeoutMs);
      this.waiters.add(waiter);
    });
  }

  sendSysex(body) {
    assertSafeToSend(body);
    this.#send(toSysex(body));
  }

  sendChannel(bytes) {
    assertSafeChannelMessage(bytes);
    this.#send(bytes);
  }

  /** Send a SysEx body and wait for a reply whose body satisfies `matchBody`. Returns the body or null. */
  async request(body, matchBody, timeoutMs = 1000) {
    const reply = this.waitFor((bytes) => {
      const b = bodyOf(bytes);
      return b !== null && matchBody(b);
    }, timeoutMs);
    this.sendSysex(body);
    const bytes = await reply;
    return bytes ? bodyOf(bytes) : null;
  }

  /** Send a SysEx body and wait for a full frame satisfying `matchFrame`. Returns the frame or null. */
  async requestFrame(body, matchFrame, timeoutMs = 1000) {
    const reply = this.waitFor(matchFrame, timeoutMs);
    this.sendSysex(body);
    return reply;
  }

  /** Run `task` after all previously queued tasks: one conversation with the G6 at a time. */
  run(task) {
    const next = this.queue.then(task, task);
    this.queue = next.catch(() => {});
    return next;
  }

  #send(bytes) {
    if (!this.attached) throw new Error('G6 not connected');
    this.output.send(bytes);
    this.#emit('log', { dir: 'out', bytes });
  }

  #onMessage(bytes) {
    this.#emit('log', { dir: 'in', bytes });
    for (const waiter of [...this.waiters]) {
      if (waiter.match(bytes)) waiter.resolve(bytes);
    }
    this.#emit('message', { bytes, body: bodyOf(bytes) });
  }

  #onStateChange() {
    const { input, output } = this.findPorts();
    const present = Boolean(input && output);
    if (!present) {
      this.input = null;
      this.output = null;
    }
    this.#emit('ports', { present });
  }

  #emit(type, detail) {
    this.dispatchEvent(new CustomEvent(type, { detail }));
  }
}
