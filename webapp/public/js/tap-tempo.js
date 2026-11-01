// Tap tempo computed in the app (the G6 has no "tap" message: GuitarLab also sends absolute BPM).

import { BPM_MIN, BPM_MAX } from './g6-protocol.js';

const RESET_AFTER_MS = 2000; // a pause longer than this starts a new measurement
const MAX_INTERVALS = 4; // average of the last intervals

export class TapTempo {
  constructor(now = () => performance.now()) {
    this.now = now;
    this.taps = [];
  }

  /** Register a tap. Returns the BPM once at least two taps are available, otherwise null. */
  tap() {
    const t = this.now();
    const last = this.taps[this.taps.length - 1];
    if (last !== undefined && t - last > RESET_AFTER_MS) this.taps = [];
    this.taps.push(t);
    if (this.taps.length > MAX_INTERVALS + 1) this.taps.shift();
    if (this.taps.length < 2) return null;
    const intervals = this.taps.slice(1).map((x, k) => x - this.taps[k]);
    const average = intervals.reduce((a, b) => a + b, 0) / intervals.length;
    const bpm = Math.round(60000 / average);
    return Math.min(BPM_MAX, Math.max(BPM_MIN, bpm));
  }

  get count() {
    return this.taps.length;
  }
}
